#!/usr/bin/env python3
"""
Example: how a stream:true client request gets answered
=============================================================

Demonstrates wire_format.py (see its own docstring for where this sits
in the architecture: providers/ handle upstream input, callers/ handle
transport, wire_format.py handles downstream output shaping, the ONLY
module allowed to know about a caller's request/response conventions,
kept out of race_proxy_core.py on purpose).

race_proxy never streams token-by-token: it always waits for a full
race to resolve before it has any answer at all. What changed is that a
client requesting stream:true now gets that complete answer wrapped in
one valid SSE data-frame chunk (plus [DONE]) instead of a flat JSON
body, the shape any OpenAI-SDK-compatible streaming client's
accumulator expects, whether that's Hermes's own auxiliary client or
anything else built against the same public spec.

Run this file directly for a standalone demo, no proxy, no network,
no API key of any kind needed:

    python3 examples/streaming_response_example.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make wire_format.py importable when run directly from examples/.
# Not needed once these are on your PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wire_format

# A fabricated, already-complete race result, exactly the shape
# race_proxy_core.py's do_POST has in hand once race() picks a winner.
# No real backend call needed to demonstrate the wire-shaping step.
FAKE_WINNING_RESPONSE = {
    "id": "chatcmpl-demo",
    "created": 1234567890,
    "model": "demo-model",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Hello from race-proxy."},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
}


def demo_non_streaming_client() -> None:
    """A client that sent stream:false (or omitted it) gets the plain
    dict back unchanged, exactly like before this feature existed.
    """
    payload = {"stream": False}
    print("client requested stream:", payload["stream"])
    print("wants_streaming_response:", wire_format.wants_streaming_response(payload))
    print("proxy answers with the plain dict as-is (unchanged path)\n")


def demo_streaming_client() -> None:
    """A client that sent stream:true (e.g. Hermes's auxiliary client
    on the compaction path, or any other OpenAI-SDK-based streaming
    consumer) gets the SSE-framed body build_sse_body produces.
    """
    payload = {"stream": True, "stream_options": {"include_usage": True}}
    print("client requested stream:", payload["stream"])
    print("wants_streaming_response:", wire_format.wants_streaming_response(payload))

    sse_body = wire_format.build_sse_body(FAKE_WINNING_RESPONSE)
    print("\n--- raw bytes written to the HTTP response ---")
    print(sse_body.decode())

    # Show what an OpenAI-SDK-style stream decoder pulls back out of
    # this, the same accumulation any spec-compliant client does.
    first_line = sse_body.decode().split("\n\n")[0]
    chunk = json.loads(first_line[len("data: "):])
    accumulated_content = chunk["choices"][0]["delta"]["content"]
    print("--- what a stream decoder accumulates ---")
    print("content:", repr(accumulated_content))
    print("usage:", chunk.get("usage"))
    assert accumulated_content == "Hello from race-proxy."
    print("\nOK, decoded content matches the original response.")


if __name__ == "__main__":
    demo_non_streaming_client()
    demo_streaming_client()
