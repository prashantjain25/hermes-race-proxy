#!/usr/bin/env python3
"""
hermes-race-proxy: core
========================

The HTTP mechanics: making a request to a backend, racing several
backends in parallel, and serving an OpenAI-compatible
``/v1/chat/completions`` endpoint that fronts them.

This module deliberately knows NOTHING about why a particular backend's
response might be broken, or how to fix it — that knowledge lives in
``repairs.py`` behind the :class:`~repairs.RepairStrategy` interface.
``Backend.call()`` below asks its :class:`~repairs.RepairRegistry` "given
this failed/unusable result, what should I retry with?" and does not
itself contain a single if/elif chain about status codes, error strings,
or response shapes. That split is intentional: this file is the part
that's the same for everyone, and ``repairs.py`` (plus whatever a user
adds via ``custom_repairs_module``) is the part that varies per vendor.

Two resource pools, shared process-wide instead of created per request
(same reasoning a database client uses for connection pooling — pay
setup cost once at startup, not per query):

- HTTP connections to each backend (``connection_pool.py`` —
  ``pooled_request``): avoids a fresh DNS+TCP+TLS handshake on every
  single chat-completion call.
- The thread pool that races backends in parallel
  (``_get_shared_executor`` below): created once on first use, reused
  for the life of the process, instead of a new ``ThreadPoolExecutor``
  spun up inside every ``race()`` call.

Importable as a library (``from race_proxy_core import Backend, race,
run_server, build_backends_from_config, load_config``) if you want to
embed the proxy in something else instead of running it standalone.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from repairs import DEFAULT_REGISTRY, RepairRegistry, load_custom_repairs
from discovery import load_and_run_discovery
from connection_pool import GLOBAL_POOL_MANAGER, pooled_request

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

logger = logging.getLogger("race_proxy")

DEFAULT_TIMEOUT = 90
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8977
MIN_PER_ATTEMPT_TIMEOUT = 5.0
"""Floor on the time budget given to any single HTTP attempt (including
repair retries), regardless of how many rungs are queued up. Prevents a
registry with many strategies from slicing the overall race timeout into
attempts too short to ever succeed."""

MAX_CONCURRENT_RACE_WORKERS = 32
"""Ceiling on the SHARED thread pool's worker count (see _SHARED_EXECUTOR
below) — not a per-request limit. A DB connection pool is sized once for
the whole application, not re-created per query; this is the same idea
applied to the thread pool that runs backend racing. Sized generously
above any realistic backend count (racing 4-8 backends per request,
possibly several concurrent requests) rather than tied 1:1 to
len(backends), because the pool is now shared across every request the
proxy serves, not scoped to one."""

_SHARED_EXECUTOR: Optional["concurrent.futures.ThreadPoolExecutor"] = None
_SHARED_EXECUTOR_LOCK = threading.Lock()


def _get_shared_executor() -> "concurrent.futures.ThreadPoolExecutor":
    """Process-wide ThreadPoolExecutor, created ONCE on first use and
    reused for every race() call for the life of the process.

    This is the thread-pool equivalent of a database connection pool
    created once at application startup rather than opened fresh for
    every query — the old code built a brand-new ThreadPoolExecutor
    inside every single race() call, paying thread-creation overhead on
    every incoming HTTP request instead of once at proxy startup.
    """
    global _SHARED_EXECUTOR
    if _SHARED_EXECUTOR is None:
        with _SHARED_EXECUTOR_LOCK:
            if _SHARED_EXECUTOR is None:  # re-check inside the lock
                _SHARED_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=MAX_CONCURRENT_RACE_WORKERS,
                    thread_name_prefix="race-proxy-worker",
                )
                logger.info(
                    "Created shared race worker pool (max_workers=%d)",
                    MAX_CONCURRENT_RACE_WORKERS,
                )
    return _SHARED_EXECUTOR


class Backend:
    """One upstream OpenAI-compatible chat-completions endpoint.

    Owns the raw HTTP mechanics (:meth:`_do_request`) and delegates all
    retry/repair decisions to its ``repairs`` registry (see
    ``repairs.py``). Swap in a different registry — via the ``repairs``
    constructor argument, or per-backend in config — to change which
    repairs run for this backend without touching this class.
    """

    __slots__ = ("name", "base_url", "model", "api_key", "headers", "repairs")

    def __init__(
        self, name: str, base_url: str, model: str, api_key: str = "",
        headers: Optional[dict] = None, repairs: Optional[RepairRegistry] = None,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        self.headers = headers or {}
        # Defaults to every registered strategy (DEFAULT_REGISTRY plus
        # anything a custom_repairs_module added to it at startup). Pass
        # an empty RepairRegistry() to disable all repairs for this
        # backend, or registry.select([...]) to opt into a subset.
        self.repairs = repairs if repairs is not None else DEFAULT_REGISTRY

    def _do_request(self, body: dict, timeout: float) -> dict:
        """One HTTP attempt against this backend, over a pooled
        connection (see connection_pool.py — same acquire/use/release
        shape as checking a connection out of a DB pool).

        Returns a dict: {"ok": bool, "backend": name, "latency": float,
        "data": <parsed json or None>, "error": <str or None>,
        "status_code": <int or None>}. This is the ONLY method in this
        file that knows how to talk HTTP to a backend — everything above
        it (repair ladders, racing) operates on this dict shape only.
        """
        path = "/chat/completions"
        headers = {"Content-Type": "application/json"}
        headers.update(self.headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            # Some keyless free tiers (e.g. OpenCode Zen free) 401 on ANY
            # Authorization header, even an empty bearer. Explicitly send
            # an empty string to override any client default.
            headers["Authorization"] = ""

        payload_bytes = json.dumps(body).encode()
        t0 = time.time()
        try:
            status, raw = pooled_request(
                base_url=self.base_url, path=path, method="POST",
                body=payload_bytes, headers=headers, timeout=timeout,
            )
            latency = time.time() - t0
            if status == 200:
                data = json.loads(raw)
                return {"ok": True, "backend": self.name, "latency": latency, "data": data,
                         "error": None, "status_code": 200}
            err_body = raw.decode(errors="replace")[:500]
            return {"ok": False, "backend": self.name, "latency": latency, "data": None,
                     "error": f"HTTP {status}: {err_body}", "status_code": status}
        except Exception as e:
            latency = time.time() - t0
            return {"ok": False, "backend": self.name, "latency": latency, "data": None,
                     "error": str(e), "status_code": None}

    def call(self, payload: dict, timeout: float) -> dict:
        """Issue the chat-completions request, applying this backend's
        repair registry to any fixable failure before giving up.

        The original attempt and every possible repair retry share one
        overall *timeout* budget, tracked as a deadline rather than
        divided into fixed slices upfront — some repairs (a boosted
        max_tokens against a slow reasoning model, observed 20-30s for
        nemotron-3.5-lightning-free) legitimately need more of the
        budget than a naive 1/N split would give them. See
        ``repairs.RepairRegistry.attempt`` for the deadline-tracking
        logic.

        Returns a dict shaped like :meth:`_do_request`'s, plus
        ``"repaired_rung"``: None if the first attempt just worked, or a
        ``"+"``-joined list of ``"<strategy>:<rung>"`` tags for whatever
        actually fixed it. See ``repairs.RepairRegistry.attempt``.
        """
        body = dict(payload)
        body["model"] = self.model

        t_start = time.monotonic()
        deadline = t_start + timeout

        first_attempt_timeout = max(timeout, MIN_PER_ATTEMPT_TIMEOUT)
        result = self._do_request(body, first_attempt_timeout)
        result["repaired_rung"] = None

        result, _final_body = self.repairs.attempt(
            do_request=lambda b, t: self._do_request(b, max(t, MIN_PER_ATTEMPT_TIMEOUT)),
            body=body,
            result=result,
            deadline=deadline,
            backend_name=self.name,
        )

        result["latency"] = time.monotonic() - t_start
        return result


def _response_is_usable(data: dict, require_finish_reason: Optional[str]) -> bool:
    """Race-level usability gate: does this WINNING candidate have real
    content and (optionally) the expected finish_reason?

    This is deliberately simpler than anything in repairs.py — it's the
    bar a response has to clear to WIN the race, not a diagnosis of why a
    losing response failed. A backend that comes back empty here just
    loses the race silently; the repair lader inside Backend.call()
    already had its chance to fix that before the result ever reaches
    here.
    """
    try:
        choice = data["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content") or ""
        if not content.strip():
            return False
        if require_finish_reason and choice.get("finish_reason") != require_finish_reason:
            return False
        return True
    except (KeyError, IndexError, TypeError):
        return False


def race(backends: list[Backend], payload: dict, timeout: float, require_finish_reason: Optional[str]) -> dict:
    """Fire all backends in parallel; return the first USABLE result.

    Each backend has already run its own repair ladder internally (see
    Backend.call) before its result ever reaches this function — so a
    backend that "wins" here already survived both its own retries AND
    this race-level usability check.

    Returns as soon as a winner is found — does NOT wait for slower
    losing backends to finish their own (possibly much longer, e.g. a
    repair-ladder retry against a slow model) attempts.

    Uses the process-wide shared executor (_get_shared_executor) rather
    than creating a new ThreadPoolExecutor per call — same reasoning as
    a database connection pool created once at startup rather than
    opened fresh per query. Because the pool is shared and long-lived,
    we must NOT call its `.shutdown()` here (that would tear down the
    pool for every other in-flight or future request); losing futures
    for this call are simply left to finish on their own in the shared
    pool's worker threads, same as a DB pool leaves an in-flight query
    running on its own connection after a caller times out waiting on
    it.
    """
    t_start = time.time()
    results_seen = []
    ex = _get_shared_executor()
    futures = {ex.submit(b.call, payload, timeout): b for b in backends}
    try:
        for fut in concurrent.futures.as_completed(futures, timeout=timeout + 5):
            b = futures[fut]
            r = fut.result()
            results_seen.append(r)
            if r["ok"] and _response_is_usable(r["data"], require_finish_reason):
                r["race_wall_clock"] = time.time() - t_start
                logger.info(
                    "race winner=%s latency=%.2fs wall_clock=%.2fs repaired_rung=%s",
                    b.name, r["latency"], r["race_wall_clock"], r.get("repaired_rung"),
                )
                return r
    except concurrent.futures.TimeoutError:
        pass

    # No usable winner — surface the most informative failure.
    wall_clock = time.time() - t_start
    logger.warning("race: no usable winner after %.2fs, %d attempt(s) seen", wall_clock, len(results_seen))
    return {
        "ok": False,
        "backend": None,
        "latency": wall_clock,
        "data": None,
        "error": f"No backend returned a usable response within {timeout}s. "
                 f"Attempts: {[(r['backend'], r['ok'], r.get('error')) for r in results_seen]}",
    }


class RaceProxyHandler(BaseHTTPRequestHandler):
    backends: list[Backend] = []
    timeout: float = DEFAULT_TIMEOUT
    require_finish_reason: Optional[str] = "stop"

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/health", "/v1/health"):
            self._send_json(200, {
                "status": "ok",
                "backends": [b.name for b in self.backends],
                "timeout": self.timeout,
                # Connection-pool stats per host, same shape a DB pool's
                # metrics endpoint typically exposes (active/idle
                # counts) — useful for spotting exhaustion before it
                # surfaces as a client-visible TimeoutError.
                "connection_pools": GLOBAL_POOL_MANAGER.stats(),
            })
            return
        if self.path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{"id": "race-proxy", "object": "model", "owned_by": "hermes-race-proxy"}],
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw)
        except Exception as e:
            self._send_json(400, {"error": f"invalid JSON body: {e}"})
            return

        result = race(self.backends, payload, self.timeout, self.require_finish_reason)
        if result["ok"]:
            data = result["data"]
            # Tag which backend actually served this, for observability
            # (non-standard field, harmless to OpenAI-compatible clients).
            data["_race_proxy"] = {
                "winner": result["backend"],
                "latency": round(result["latency"], 3),
                "repaired_rung": result.get("repaired_rung"),
            }
            self._send_json(200, data)
        else:
            self._send_json(502, {"error": {"message": result["error"], "type": "race_proxy_all_backends_failed"}})


def load_config(path: Optional[str]) -> dict:
    if path is None:
        return {}
    with open(path, "r") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        if yaml is None:
            print("PyYAML not installed; install with `pip install pyyaml` or use a .json config", file=sys.stderr)
            sys.exit(1)
        return yaml.safe_load(text) or {}
    return json.loads(text)


def build_registry(cfg: dict) -> RepairRegistry:
    """Build the master repair registry for this run: the built-in
    strategies (structured-output relaxation, token-starvation boost)
    plus anything loaded from ``custom_repairs_module`` in config.

    Per-backend selection (``backends[].repairs: [...]``) happens later,
    in :func:`build_backends_from_config`, by filtering this master
    registry with :meth:`RepairRegistry.select`.
    """
    custom_module = cfg.get("custom_repairs_module")
    if custom_module:
        load_custom_repairs(custom_module, DEFAULT_REGISTRY)
    return DEFAULT_REGISTRY


def build_backends_from_config(cfg: dict, registry: Optional[RepairRegistry] = None) -> list[Backend]:
    registry = registry if registry is not None else build_registry(cfg)
    backends = []
    for entry in cfg.get("backends", []):
        repair_names = entry.get("repairs")
        backend_registry = (
            registry.select(repair_names) if repair_names is not None else registry
        )
        backends.append(Backend(
            name=entry["name"],
            base_url=entry["base_url"],
            model=entry["model"],
            api_key=entry.get("api_key", ""),
            headers=entry.get("headers", {}),
            repairs=backend_registry,
        ))
    return backends


def run_server(cfg: dict, host: Optional[str] = None, port: Optional[int] = None,
                timeout: Optional[float] = None) -> ThreadingHTTPServer:
    """Build and start the proxy's HTTP server from a config dict.

    Backend selection: if ``cfg['custom_discovery_module']`` is set, its
    ``discover_backends(cfg)`` runs ONCE here (see ``discovery.py``) and
    its result is used; otherwise (or on any failure in a custom
    module) the static ``backends:`` list from config is used, exactly
    as before this feature existed. Discovery is a startup-time cost
    only — it never runs per-request, so an exhaustive probe of a dozen
    candidate models to pick the fastest few is a reasonable thing to do
    there.

    Returns the (already-``serve_forever``-ready but not yet serving)
    server object — callers own the run loop, so this is usable both by
    the CLI entrypoint (``race_proxy.py``) and by anything embedding the
    proxy as a library.
    """
    host = host or cfg.get("host", DEFAULT_HOST)
    port = port or cfg.get("port", DEFAULT_PORT)
    timeout = timeout or cfg.get("timeout", DEFAULT_TIMEOUT)
    require_finish_reason = cfg.get("require_finish_reason", "stop")

    backends = load_and_run_discovery(
        cfg, build_static_backends=lambda: build_backends_from_config(cfg),
    )
    if not backends:
        raise RuntimeError(
            "No backends configured. Provide a config with a `backends:` list, "
            "or a working `custom_discovery_module`."
        )

    RaceProxyHandler.backends = backends
    RaceProxyHandler.timeout = timeout
    RaceProxyHandler.require_finish_reason = require_finish_reason

    server = ThreadingHTTPServer((host, port), RaceProxyHandler)
    logger.info(
        "hermes-race-proxy listening on http://%s:%s — racing backends: %s (timeout=%ss)",
        host, port, ", ".join(b.name for b in backends), timeout,
    )
    return server
