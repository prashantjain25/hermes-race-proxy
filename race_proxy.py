#!/usr/bin/env python3
"""
hermes-race-proxy: legacy single-process entrypoint (deprecated split)
============================================================================

This file is kept for backward compatibility with any existing config
or launch script pointing at it directly, but for a Hermes deployment
with more than one auxiliary task (compression + mcp/skills_hub/
title_generation), prefer the split entrypoints instead:

  race_proxy_toolchain.py , mcp/skills_hub/title_generation, short
                             timeout, default port 8978.
  race_proxy_compaction.py , context compression only, long timeout,
                              default port 8977 (same port this file
                              used, so an existing config pointing here
                              can switch to race_proxy_compaction.py
                              with zero config changes).

See race_proxy_toolchain.py's docstring for the real, observed reason
they're split: sharing one port/timeout/connection-pool between a
300s-tolerant compaction call and a fail-fast toolchain call meant a
slow compaction attempt could starve or delay an unrelated toolchain
call queued behind it on the same shared worker pool.

Running this file directly still works exactly as before (one
process, one port, one shared timeout for every task pointed at it) —
it is simply no longer the RECOMMENDED shape for a multi-task Hermes
deployment.

Original docstring (behavior unchanged below this point):

A tiny local OpenAI-compatible HTTP proxy that fans a single
`/v1/chat/completions` request out to N upstream backends in parallel and
returns whichever finishes first with a usable (non-empty) response. Also
repairs a class of fixable failures per-backend before giving up on it
(structured-output rejections, reasoning-token starvation), see
``repairs.py`` for that logic and how to add your own.

Built for Hermes Agent auxiliary-task routing (skills_hub, mcp, approval,
title_generation, etc.) where `auxiliary.<task>.fallback_chain` is
strictly SEQUENTIAL (try primary, then on failure/timeout try the next
entry). This proxy gives you a RACE instead: fire all configured backends
at once, take the fastest valid completion, ignore the rest.

Point any OpenAI-compatible client (including Hermes, via a custom
`base_url` provider entry) at this proxy's `/v1` endpoint instead of
directly at a single backend.

This file is a thin CLI wrapper. The actual logic lives in several other
modules, split by concern so each is independently reusable/extensible:

  race_proxy_core.py , HTTP mechanics: making requests, racing backends,
                         serving the endpoint. No knowledge of *why* a
                         response might be broken.
  repairs.py          , WHY a response might be broken and how to fix
                          it, behind one small RepairStrategy interface.
                          This is where you plug in support for your own
                          model/vendor's failure shape without editing
                          anything else. See repairs.py's own docstring
                          for a step-by-step, and
                          examples/custom_repairs_example.py for a
                          runnable template.
  response_contracts.py , WAS a 200 response actually usable, per
                            backend, behind a small ProviderContract
                            interface (an Adapter, see that module's
                            docstring for why). Almost every backend
                            uses the generic contract; a new one is
                            written only when a real observed response
                            shape needs it.
  callers/             , HOW to reach a backend at all (pooled HTTP by
                          default, or a CLI subprocess for a vendor
                          with no HTTP API) — orthogonal to whether the
                          response was usable, see callers/base.py.

Usage:
    python3 race_proxy.py --config race_proxy.yaml
    # or environment-var driven, see README


Config format (YAML or JSON), see race_proxy.example.json/.yaml for a
ready-to-copy template:
    host: 127.0.0.1
    port: 8977
    timeout: 90            # seconds, per-backend race timeout
    require_finish_reason: stop   # only accept completions with this
                                   # finish_reason (reasoning models can
                                   # burn max_tokens on hidden reasoning
                                   # and return empty content otherwise)
    custom_repairs_module: /path/to/my_repairs.py   # optional, see repairs.py
    backends:
      - name: backend-a
        base_url: https://your-provider-a.example.com/v1
        model: your-model-a
        api_key: ""          # empty string = no Authorization header sent
        headers: {}           # any extra headers your provider requires
        repairs: [format, tokens]   # optional: restrict which registered
                                     # repair strategies run for THIS
                                     # backend. Omit to use all of them.
      - name: backend-b
        base_url: https://your-provider-b.example.com/v1
        model: your-model-b
        api_key: ""
        headers: {}

Security note: this proxy has NO authentication of its own by default,
it is meant to be bound to 127.0.0.1 and used locally. Do not expose it
on a public interface without adding your own auth layer.
"""
from __future__ import annotations

import argparse
import logging
import sys

from race_proxy_core import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TIMEOUT, load_config, run_server

logger = logging.getLogger("race_proxy")


def main():
    parser = argparse.ArgumentParser(description="Race N OpenAI-compatible backends, serve the fastest usable reply.")
    parser.add_argument("--config", "-c", default=None, help="Path to YAML or JSON config file.")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", "-p", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=None, help="Per-race timeout in seconds.")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--log-file", default=None,
                        help="Append logs to this file (in addition to stderr). "
                             "Use when launching with stdout/stderr detached.")
    args = parser.parse_args()

    handlers: list = [logging.StreamHandler()]
    if args.log_file:
        import os
        os.makedirs(os.path.dirname(os.path.expanduser(args.log_file)) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(os.path.expanduser(args.log_file)))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )

    cfg = load_config(args.config)

    try:
        server = run_server(
            cfg,
            host=args.host or cfg.get("host", DEFAULT_HOST),
            port=args.port or cfg.get("port", DEFAULT_PORT),
            timeout=args.timeout or cfg.get("timeout", DEFAULT_TIMEOUT),
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
