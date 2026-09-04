#!/usr/bin/env python3
"""
hermes-race-proxy: compaction entrypoint (context compression only)
========================================================================

Companion to race_proxy_toolchain.py, see that file's docstring for
the full reasoning behind splitting one port into two. Short version:
compaction calls are large and genuinely slow, real observed latency
in this proxy's own logs includes a 702.84s single successful call and
several 100-250s calls, all against the SAME backends the toolchain
proxy also uses. Sharing one port/timeout/connection-pool between
compaction and small latency-sensitive toolchain calls meant a slow
compaction attempt could starve or delay a toolchain call queued
behind it on the same worker pool, and a timeout tuned for compaction
(300s) was far too generous for a toolchain call that should fail fast
instead.

This process keeps the ORIGINAL port (8977) that the single, unsplit
race_proxy.py used before this split, specifically so an existing
``auxiliary.compression`` config entry pointing at port 8977 keeps
working with zero edits. The code-level default timeout is unchanged
too (90s, race_proxy_core.DEFAULT_TIMEOUT) — a real compaction
deployment typically overrides this explicitly in its config file
(e.g. ``timeout: 300`` in race_proxy.local.yaml) rather than relying
on the code default, since 700+ second real observed latencies (see
above) need a config-level override regardless of which entrypoint
serves the request. Only ``auxiliary.mcp`` / ``auxiliary.skills_hub``
/ ``auxiliary.title_generation`` need to move to
race_proxy_toolchain.py's port (default 8978).

Usage:
    python3 race_proxy_compaction.py --config race_proxy_compaction.json
"""
from __future__ import annotations

import argparse
import logging
import sys

from race_proxy_core import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TIMEOUT, load_config, run_server

logger = logging.getLogger("race_proxy.compaction")


def main():
    parser = argparse.ArgumentParser(
        description="Race N OpenAI-compatible backends for Hermes context "
                     "compaction only — long timeout, separate process from "
                     "toolchain tasks. See this file's own docstring for why "
                     "they're split.")
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
