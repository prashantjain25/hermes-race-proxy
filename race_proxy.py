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


class Backend:
    __slots__ = ("name", "base_url", "model", "api_key", "headers")

    def __init__(self, name: str, base_url: str, model: str, api_key: str = "", headers: Optional[dict] = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        self.headers = headers or {}

    def call(self, payload: dict, timeout: float) -> dict:
        """Issue the chat-completions request against this backend.

        Returns a dict: {"ok": bool, "backend": name, "latency": float,
        "data": <parsed json or None>, "error": <str or None>}.
        """
        body = dict(payload)
        body["model"] = self.model
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
            return {"ok": True, "backend": self.name, "latency": latency, "data": data, "error": None}
        except urllib.error.HTTPError as e:
            latency = time.time() - t0
            try:
                err_body = e.read().decode()[:500]
            except Exception:
                err_body = str(e)
            return {"ok": False, "backend": self.name, "latency": latency, "data": None, "error": f"HTTP {e.code}: {err_body}"}
        except Exception as e:
            latency = time.time() - t0
            return {"ok": False, "backend": self.name, "latency": latency, "data": None, "error": str(e)}


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
            data["_race_proxy"] = {"winner": result["backend"], "latency": round(result["latency"], 3)}
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
