#!/usr/bin/env python3
"""
hermes-race-proxy
==================

A tiny local OpenAI-compatible HTTP proxy that fans a single
`/v1/chat/completions` request out to N upstream backends in parallel and
returns whichever finishes first with a usable (non-empty) response.

Built for Hermes Agent auxiliary-task routing (skills_hub, mcp, approval,
etc.) where `auxiliary.<task>.fallback_chain` is strictly SEQUENTIAL
(try primary, then on failure/timeout try the next entry). This proxy
gives you a RACE instead: fire all configured backends at once, take the
fastest valid completion, ignore the rest.

Point any OpenAI-compatible client (including Hermes, via a custom
`base_url` provider entry) at this proxy's `/v1` endpoint instead of
directly at a single backend.

Usage:
    python3 race_proxy.py --config race_proxy.yaml
    # or environment-var driven, see README

Config format (YAML or JSON) — see race_proxy.example.json/.yaml for a
ready-to-copy template:
    host: 127.0.0.1
    port: 8977
    timeout: 90            # seconds, per-backend race timeout
    require_finish_reason: stop   # only accept completions with this
                                   # finish_reason (reasoning models can
                                   # burn max_tokens on hidden reasoning
                                   # and return empty content otherwise)
    backends:
      - name: backend-a
        base_url: https://your-provider-a.example.com/v1
        model: your-model-a
        api_key: ""          # empty string = no Authorization header sent
        headers: {}           # any extra headers your provider requires
        repair_structured_output: true   # auto-relax response_format on 400/422
        repair_token_starvation: true    # auto-boost max_tokens on empty+length
      - name: backend-b
        base_url: https://your-provider-b.example.com/v1
        model: your-model-b
        api_key: ""
        headers: {}

Security note: this proxy has NO authentication of its own by default —
it is meant to be bound to 127.0.0.1 and used locally. Do not expose it
on a public interface without adding your own auth layer.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None

logger = logging.getLogger("race_proxy")

DEFAULT_TIMEOUT = 90
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8977


def _relax_response_format(body: dict, rung: int) -> Optional[dict]:
    """Return *body* with its ``response_format`` progressively loosened.

    Many OpenAI-compatible gateways (free-tier aggregators especially)
    advertise Chat Completions compatibility but reject strict JSON-Schema
    structured output with an opaque 400 that never mentions
    ``response_format`` by name (e.g. a generic
    ``"Upstream request failed: [400] Provider returned error"``). A caller
    can't reliably string-match its way to "this was a structured-output
    rejection" across every vendor's error envelope shape — so instead of
    trying to *detect* the cause, we just try looser contracts in order
    whenever the response was a 400/422 AND the request carried a
    ``response_format``. This is model-agnostic and vendor-agnostic: it
    doesn't matter which backend or model produced the 400, the ladder is
    the same.

    Rung 0: original body, untouched (the caller's first attempt).
    Rung 1: ``response_format.json_schema.strict`` forced to False, schema
             kept. Some vendors support json_schema mode but reject strict
             enforcement specifically (uneven ``strict: true`` support is a
             documented gap across Together/Groq/Fireworks-style compat
             layers).
    Rung 2: ``response_format`` stripped entirely. Schema enforcement
             degrades to whatever the system/user prompt asked for in
             plain text — the caller's own response parser needs a
             loose-JSON-scan fallback for this to still work (Hermes's
             ``title_generator._extract_title_text`` already has one).

    Returns None once there is nothing left to relax (all rungs exhausted).
    """
    rf = body.get("response_format")
    if not isinstance(rf, dict):
        return None  # no response_format in this request; nothing to relax
    if rung == 1:
        if rf.get("type") != "json_schema":
            return None
        json_schema = rf.get("json_schema")
        if not isinstance(json_schema, dict) or json_schema.get("strict") is not True:
            return None  # already non-strict or no strict flag to drop
        new_body = dict(body)
        new_rf = dict(rf)
        new_json_schema = dict(json_schema)
        new_json_schema["strict"] = False
        new_rf["json_schema"] = new_json_schema
        new_body["response_format"] = new_rf
        return new_body
    if rung == 2:
        new_body = dict(body)
        new_body.pop("response_format", None)
        return new_body
    return None


class Backend:
    __slots__ = (
        "name", "base_url", "model", "api_key", "headers",
        "repair_structured_output", "repair_token_starvation",
    )

    def __init__(
        self, name: str, base_url: str, model: str, api_key: str = "",
        headers: Optional[dict] = None, repair_structured_output: bool = True,
        repair_token_starvation: bool = True,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        self.headers = headers or {}
        # See _relax_response_format(). On by default: it's a no-op unless
        # the request actually carries a response_format, so it's safe to
        # leave on for backends that never use structured output.
        self.repair_structured_output = repair_structured_output
        # See _looks_token_starved(). On by default: it's a no-op unless
        # the response actually comes back empty with finish_reason
        # "length", so it's safe to leave on for non-reasoning backends
        # too (they simply never trigger it).
        self.repair_token_starvation = repair_token_starvation

    def _do_request(self, body: dict, timeout: float) -> dict:
        """One raw HTTP attempt. Returns the same shape as call()."""
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        headers.update(self.headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            # Some keyless free tiers (e.g. OpenCode Zen free) 401 on ANY
            # Authorization header, even an empty bearer. Explicitly send
            # an empty string to override any client default.
            headers["Authorization"] = ""

        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw)
            latency = time.time() - t0
            return {"ok": True, "backend": self.name, "latency": latency, "data": data,
                     "error": None, "status_code": 200}
        except urllib.error.HTTPError as e:
            latency = time.time() - t0
            try:
                err_body = e.read().decode()[:500]
            except Exception:
                err_body = str(e)
            return {"ok": False, "backend": self.name, "latency": latency, "data": None,
                     "error": f"HTTP {e.code}: {err_body}", "status_code": e.code}
        except Exception as e:
            latency = time.time() - t0
            return {"ok": False, "backend": self.name, "latency": latency, "data": None,
                     "error": str(e), "status_code": None}

    def call(self, payload: dict, timeout: float) -> dict:
        """Issue the chat-completions request against this backend.

        Retries through two independent repair ladders before giving up:

        1. Structured-output relaxation (_relax_response_format) on a
           400/422, when ``repair_structured_output`` is enabled and the
           request carries a ``response_format``.
        2. Token-budget boost (_looks_token_starved) on a 200 whose
           content came back empty because max_tokens was too small for
           the model's reasoning overhead, when ``repair_token_starvation``
           is enabled.

        Both are model-agnostic: neither depends on which backend or model
        is behind this call, only on the shape of the request/response.
        Time spent on retries counts against the caller's overall race
        timeout (each attempt gets an even share).

        Returns a dict: {"ok": bool, "backend": name, "latency": float,
        "data": <parsed json or None>, "error": <str or None>,
        "repaired_rung": <str|None>}. ``repaired_rung`` is one of
        "format:1", "format:2", "tokens", or None (no repair needed).
        """
        body = dict(payload)
        body["model"] = self.model

        max_attempts = 1
        if self.repair_structured_output:
            max_attempts += 2
        if self.repair_token_starvation:
            max_attempts += 1
        per_attempt_timeout = max(timeout / max_attempts, 5.0)

        t_start = time.time()
        result = self._do_request(body, per_attempt_timeout)
        result["repaired_rung"] = None

        # ── Ladder 1: structured-output relaxation (400/422 only) ──
        if not result["ok"] and self.repair_structured_output and (
            result.get("status_code") in (400, 422)
        ):
            for rung in (1, 2):
                relaxed_body = _relax_response_format(body, rung)
                if relaxed_body is None:
                    continue
                logger.info(
                    "%s: 400/422 on original request, retrying with "
                    "response_format relaxed (rung %d)", self.name, rung,
                )
                retry_result = self._do_request(relaxed_body, per_attempt_timeout)
                if retry_result["ok"]:
                    retry_result["repaired_rung"] = f"format:{rung}"
                    result = retry_result
                    body = relaxed_body  # the body that actually worked — ladder 2 must build on this, not the original
                    break
                result = retry_result
                result["repaired_rung"] = None
                if result.get("status_code") not in (400, 422):
                    break
                body = relaxed_body  # carry the relaxation forward for the next rung

        # ── Ladder 2: token-budget boost (200-but-starved only) ──
        if result["ok"] and self.repair_token_starvation and _looks_token_starved(result["data"]):
            current_max_tokens = body.get("max_tokens")
            if current_max_tokens is None or current_max_tokens < MIN_SAFE_MAX_TOKENS:
                boosted_body = dict(body)
                boosted_body["max_tokens"] = MIN_SAFE_MAX_TOKENS
                logger.info(
                    "%s: response starved (empty content, finish_reason=length) "
                    "at max_tokens=%s, retrying with max_tokens=%d",
                    self.name, current_max_tokens, MIN_SAFE_MAX_TOKENS,
                )
                retry_result = self._do_request(boosted_body, per_attempt_timeout)
                if retry_result["ok"] and not _looks_token_starved(retry_result["data"]):
                    prior_rung = result.get("repaired_rung")
                    retry_result["repaired_rung"] = (
                        f"{prior_rung}+tokens" if prior_rung else "tokens"
                    )
                    result = retry_result
                else:
                    logger.warning(
                        "%s: max_tokens boost to %d did not resolve starvation "
                        "(ok=%s, error=%s)", self.name, MIN_SAFE_MAX_TOKENS,
                        retry_result["ok"], retry_result.get("error"),
                    )

        result["latency"] = time.time() - t_start
        return result


MIN_SAFE_MAX_TOKENS = 2000
"""Floor for a boosted retry when a low max_tokens starves reasoning models.

Reasoning models (ling, nemotron, and similar) spend part of their
completion budget on hidden ``reasoning`` tokens before ever writing
visible ``content``. A caller that sets a small max_tokens for a
short-answer task (Hermes's title_generator uses 64, expecting "a title is
a handful of tokens") can starve the model completely: all budget goes to
reasoning, content is empty, finish_reason is "length". This is a
model-behavior problem, not a per-caller bug — no amount of prompt tuning
fixes it, because the model doesn't know its own budget is too small
until it has already spent it. The fix is a bigger budget, tried
automatically. See references/auxiliary-compression-benchmarks.md for the
benchmark data behind this floor (reasoning consumed the whole budget
below ~1500 tokens in production trials; 2000-4000 is the safe range)."""


def _response_is_usable(data: dict, require_finish_reason: Optional[str]) -> bool:
    """Reject empty-content responses (common with reasoning models that
    burn their whole max_tokens budget on hidden reasoning tokens)."""
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


def _looks_token_starved(data: Optional[dict]) -> bool:
    """True when a 200 response is empty because reasoning ate the budget.

    Distinguishes "the model produced nothing because max_tokens was too
    small for reasoning + content" (fixable by raising max_tokens) from
    other reasons a response might be unusable (e.g. the model just
    refused, or `require_finish_reason` rejected a legitimately truncated
    answer for other reasons). We only call this a starvation case when
    BOTH signals line up: empty/whitespace content AND finish_reason ==
    "length" — a model that stopped naturally (finish_reason "stop") with
    empty content has a different problem that a bigger budget won't fix.
    """
    if not isinstance(data, dict):
        return False
    try:
        choice = data["choices"][0]
        msg = choice.get("message", {})
        content = (msg.get("content") or "").strip()
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return False
    return not content and finish_reason == "length"


def race(backends: list[Backend], payload: dict, timeout: float, require_finish_reason: Optional[str]) -> dict:
    """Fire all backends in parallel; return the first USABLE result.

    If a backend returns fast but with empty/unusable content (e.g. a
    reasoning model that burned its token budget on hidden reasoning),
    we keep waiting on the others rather than accepting garbage.
    """
    t_start = time.time()
    results_seen = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(backends))) as ex:
        futures = {ex.submit(b.call, payload, timeout): b for b in backends}
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=timeout + 5):
                b = futures[fut]
                r = fut.result()
                results_seen.append(r)
                if r["ok"] and _response_is_usable(r["data"], require_finish_reason):
                    r["race_wall_clock"] = time.time() - t_start
                    logger.info(
                        "race winner=%s latency=%.2fs wall_clock=%.2fs",
                        b.name, r["latency"], r["race_wall_clock"],
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


def build_backends_from_config(cfg: dict) -> list[Backend]:
    backends = []
    for entry in cfg.get("backends", []):
        backends.append(Backend(
            name=entry["name"],
            base_url=entry["base_url"],
            model=entry["model"],
            api_key=entry.get("api_key", ""),
            headers=entry.get("headers", {}),
            repair_structured_output=entry.get("repair_structured_output", True),
            repair_token_starvation=entry.get("repair_token_starvation", True),
        ))
    return backends


def main():
    parser = argparse.ArgumentParser(description="Race N OpenAI-compatible backends, serve the fastest usable reply.")
    parser.add_argument("--config", "-c", default=None, help="Path to YAML or JSON config file.")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", "-p", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=None, help="Per-race timeout in seconds.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)
    host = args.host or cfg.get("host", DEFAULT_HOST)
    port = args.port or cfg.get("port", DEFAULT_PORT)
    timeout = args.timeout or cfg.get("timeout", DEFAULT_TIMEOUT)
    require_finish_reason = cfg.get("require_finish_reason", "stop")

    backends = build_backends_from_config(cfg)
    if not backends:
        print("No backends configured. Provide --config pointing at a YAML/JSON file with a `backends:` list.", file=sys.stderr)
        sys.exit(1)

    RaceProxyHandler.backends = backends
    RaceProxyHandler.timeout = timeout
    RaceProxyHandler.require_finish_reason = require_finish_reason

    server = ThreadingHTTPServer((host, port), RaceProxyHandler)
    logger.info(
        "hermes-race-proxy listening on http://%s:%s — racing backends: %s (timeout=%ss)",
        host, port, ", ".join(b.name for b in backends), timeout,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
