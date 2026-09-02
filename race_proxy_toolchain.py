#!/usr/bin/env python3
"""
hermes-race-proxy: toolchain entrypoint (mcp / skills_hub / title_generation)
=================================================================================

Why this file exists, separate from race_proxy_compaction.py
------------------------------------------------------------------
Both entrypoints share 100% of the actual logic
(race_proxy_core.py / connection_pool.py / repairs.py /
response_contracts.py / callers/) — this file and
race_proxy_compaction.py are thin wrappers that differ only in their
DEFAULT port and timeout, the same "thin CLI wrapper over shared core"
shape race_proxy.py itself already used before this split, just with
two default profiles instead of one.

The reason they need to be separate PROCESSES (not just separate
config sections behind one port) is a real, observed problem: before
this split, every Hermes auxiliary task (compression, skills_hub, mcp,
title_generation) pointed at the SAME port (8977), meaning they shared
one process's connection pool, one shared worker thread pool, and
critically one ``timeout`` value. Compaction legitimately needs a
large timeout (a 700+ second real observed latency on a big
summarization call, see race_proxy_compaction.py's own docstring for
the log evidence), but title_generation/mcp/skills_hub are small,
latency-sensitive calls that should fail fast and hand off to Hermes's
own fallback_chain quickly, not inherit a 300-second budget meant for
a completely different class of request. Splitting by PORT (not just
by config timeout override per task, which race_proxy_core.py's
RaceProxyHandler doesn't support per-request anyway, ``timeout`` is
process-wide) is what actually isolates one task's slow backend from
starving another task's fast one.

Point ``auxiliary.mcp``, ``auxiliary.skills_hub``, and
``auxiliary.title_generation`` in config.yaml at THIS process's port
(default 8978). Point ``auxiliary.compression`` at
race_proxy_compaction.py's port (default 8977, unchanged from before
the split, so existing compaction config keeps working without an
edit) — see race_proxy_compaction.py.

Usage:
    python3 race_proxy_toolchain.py --config race_proxy_toolchain.json
"""
from __future__ import annotations

import argparse
import logging
import sys

from race_proxy_core import load_config, run_server

logger = logging.getLogger("race_proxy.toolchain")

#: Toolchain calls are small and latency-sensitive (a title, an MCP
#: sampling response, a skills-hub lookup) — fail fast rather than
#: burning a compaction-sized timeout budget on a task the CALLER is
#: waiting synchronously on. Override in config/CLI if your toolchain
#: backends genuinely need longer.
DEFAULT_TOOLCHAIN_TIMEOUT = 60
DEFAULT_TOOLCHAIN_PORT = 8978


def main():
    parser = argparse.ArgumentParser(
        description="Race N OpenAI-compatible backends for Hermes toolchain "
                     "tasks (mcp/skills_hub/title_generation) — short timeout, "
                     "separate process from compaction. See this file's own "
                     "docstring for why they're split.")
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
            host=args.host or cfg.get("host", "127.0.0.1"),
            port=args.port or cfg.get("port", DEFAULT_TOOLCHAIN_PORT),
            timeout=args.timeout or cfg.get("timeout", DEFAULT_TOOLCHAIN_TIMEOUT),
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
