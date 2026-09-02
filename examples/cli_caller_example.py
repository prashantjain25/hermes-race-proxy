#!/usr/bin/env python3
"""
Example: a CLI-only backend racing alongside your HTTP backends
====================================================================

Demonstrates the ``callers/`` Strategy split (see callers/base.py):
Backend doesn't care HOW its bytes get fetched, HTTP socket vs CLI
subprocess, as long as whatever Caller you give it returns the same
``(status, raw_bytes)`` shape. This lets a vendor with no public HTTP
chat-completions API (CLI-only, "the devpass case" per callers/base.py)
race in the exact same pool as your normal HTTP backends, no special
casing anywhere in race_proxy_core.py, repairs.py, or
response_contracts.py.

This example uses ``echo`` as a stand-in CLI so it's runnable with zero
setup and NO API key of any kind, real or fake. Swap ``COMMAND_TEMPLATE``
and RESPONSE_PARSER for your actual vendor's CLI invocation, most CLIs
read the prompt from stdin and print a response on stdout, see
callers/cli_caller.py's docstring for the stdin-vs-argv-vs-file-path
tradeoff if your CLI doesn't fit that shape.

Run this file directly for a standalone demo (no proxy needed):

    python3 examples/cli_caller_example.py

Wire a real CLI-only vendor into your proxy config by constructing a
Backend with a CliCaller the same way build_backends_from_config does
for HTTP backends, there's no config-file syntax for this yet since
every real CLI's argv shape differs; do it in a small Python entrypoint
(see race_proxy_toolchain.py / race_proxy_compaction.py for the pattern
of a thin script that builds backends and starts the server) or a
custom_discovery_module (see custom_discovery_example.py) that appends
a CliCaller-backed Backend to the returned list.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make callers/, race_proxy_core.py importable when run directly from
# examples/. Not needed once these are on your PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from callers.cli_caller import CliCaller
from race_proxy_core import Backend


def echo_response_parser(stdout: bytes, stderr: bytes, returncode: int) -> tuple[int, bytes]:
    """Stand-in for a real vendor's response_parser. echo just prints
    back whatever we sent it, so we wrap that raw text into a minimal
    OpenAI chat.completion shape response_contracts.py's
    GenericOpenAIContract already knows how to read, same as any real
    HTTP backend's response. A real CLI's own parser instead reads
    whatever that CLI actually prints, see default_response_parser in
    callers/cli_caller.py for the "already prints JSON" case this one
    is deliberately NOT using, to show the "CLI prints plain text"
    branch instead.
    """
    if returncode != 0:
        err = stderr.decode(errors="replace")[:500]
        return 500, json.dumps({"error": {"message": err, "type": "cli_error"}}).encode()
    text = stdout.decode(errors="replace").strip()
    fake_completion = {
        "id": "cli-demo-0",
        "model": "echo-demo",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
    }
    return 200, json.dumps(fake_completion).encode()


# echo just prints its argv back; a real CLI here is something like
# ["your-vendor-cli", "chat", "--stdin"] that reads the request off
# stdin the way callers/cli_caller.py's CliCaller always feeds it.
COMMAND_TEMPLATE = ["echo", "hello from a CLI-only backend"]


def build_demo_backend() -> Backend:
    caller = CliCaller(
        command_template=COMMAND_TEMPLATE,
        response_parser=echo_response_parser,
    )
    return Backend(
        name="cli-demo",
        base_url="cli://echo-demo",  # cosmetic only, CliCaller ignores it
        model="echo-demo",
        caller=caller,
    )


if __name__ == "__main__":
    backend = build_demo_backend()
    demo_body = {
        "model": backend.model,
        "messages": [{"role": "user", "content": "ping"}],
    }
    result = backend._do_request(demo_body, timeout=10.0)
    print("ok:", result.get("ok"))
    print("raw result:", result)
