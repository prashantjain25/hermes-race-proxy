#!/usr/bin/env python3
"""
hermes-race-proxy: backend discovery
======================================

Optional, pluggable BACKEND SELECTION at proxy startup — kept out of
core for the same reason repairs.py's strategies are pluggable: which
models you probe, how you rank them, which providers you trust with
your keys, and how many backends you want racing is a deeply personal
policy call (your own credentials, your own vendor priorities, your own
reliability tradeoffs) — not something that belongs hardcoded into a
project other people install.

DEFAULT BEHAVIOR (no configuration needed): the proxy reads its static
`backends:` list straight out of config, exactly as it always has. This
module and its extension point only activate when you explicitly set
`custom_discovery_module` in your config — if you never touch this,
nothing changes and this file is never imported for anything but its
loader.

Quick start — bring your own backend-selection policy
--------------------------------------------------------
1. Write a standalone .py file anywhere on disk (it does not need to
   live in this repo — see examples/custom_discovery_example.py for a
   complete, runnable template implementing a real ranked-selection
   policy: two fixed OpenAI-compatible backends always included, plus
   the top N candidates from an exhaustive startup probe of another
   provider's catalog).
2. Define a module-level function:

       def discover_backends(cfg: dict) -> list[Backend]:
           ...

   Import `Backend` from `race_proxy_core` and construct/return however
   many Backend instances you want the proxy to race. This runs ONCE at
   proxy startup, not per-request — expensive probing here (hitting a
   dozen candidate models to rank them) is fine, it never adds latency
   to a real chat-completion call.
3. Point your proxy config at it:

       {"custom_discovery_module": "/path/to/my_discovery.py", "backends": [...]}

   The static `backends:` list stays in config as a DOCUMENTED FALLBACK
   — see load_and_run_discovery() below for the exact contract: if
   `custom_discovery_module` is unset, fails to load, or its
   discover_backends() raises or returns nothing, the proxy logs a
   warning and falls back to the static `backends:` list untouched. A
   broken discovery script degrades to "no discovery," never to
   "no backends at all."
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable, List, Optional

logger = logging.getLogger("race_proxy.discovery")


def probe_endpoint(
    base_url: str,
    model: str,
    api_key: str = "",
    headers: Optional[dict] = None,
    timeout: float = 15.0,
    probe_payload: Optional[dict] = None,
) -> dict:
    """Make one lightweight real HTTP call to check if a model is alive
    and how fast it responds.

    Returns ``{"ok": bool, "latency": float, "error": str|None}``. A
    generic, vendor-agnostic building block — a plain chat-completion
    request with a tiny prompt and small max_tokens, timed — for use
    inside your own :func:`discover_backends` implementation instead of
    hand-rolling raw ``urllib`` calls. See
    ``examples/custom_discovery_example.py`` for it in real use,
    ranking candidate models by measured latency and success.

    Not invoked by anything in this module automatically; it's exported
    purely as a convenience for custom discovery scripts.
    """
    payload = dict(probe_payload or {
        "messages": [{"role": "user", "content": "Reply with the single word OK"}],
        "max_tokens": 20,
    })
    payload["model"] = model
    url = f"{base_url.rstrip('/')}/chat/completions"
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers or {})
    req_headers.setdefault("Authorization", f"Bearer {api_key}" if api_key else "")

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=req_headers, method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return {"ok": True, "latency": time.time() - t0, "error": None}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:300]
        except Exception:
            body = str(e)
        return {"ok": False, "latency": time.time() - t0, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"ok": False, "latency": time.time() - t0, "error": str(e)}


def load_and_run_discovery(cfg: dict, build_static_backends: Callable[[], List]) -> List:
    """Load ``cfg['custom_discovery_module']`` if set and run its
    ``discover_backends(cfg)``; fall back to *build_static_backends()*
    on any failure — missing config key, module load error, or an
    exception raised inside the user's own ``discover_backends``.

    *build_static_backends* is a zero-arg callable the caller supplies
    (normally :func:`race_proxy_core.build_backends_from_config` bound
    to *cfg*) rather than this module importing ``race_proxy_core``
    directly — ``race_proxy_core`` imports THIS module to call this
    function, so a direct import back would be circular.
    """
    module_path = cfg.get("custom_discovery_module")
    if not module_path:
        return build_static_backends()

    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_race_proxy_custom_discovery", module_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load custom discovery module: {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "discover_backends"):
            raise RuntimeError(
                f"{module_path} must define a module-level "
                f"discover_backends(cfg: dict) -> list[Backend] function"
            )
        backends = module.discover_backends(cfg)
        if not backends:
            raise RuntimeError("discover_backends() returned an empty list")
        logger.info(
            "Loaded %d backend(s) from custom discovery module %s: %s",
            len(backends), module_path, ", ".join(b.name for b in backends),
        )
        return backends
    except Exception:
        logger.warning(
            "Custom discovery module %s failed to load or run; falling back "
            "to the static `backends:` list in config",
            module_path, exc_info=True,
        )
        return build_static_backends()
