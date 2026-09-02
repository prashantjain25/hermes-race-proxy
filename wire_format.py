"""
wire_format.py — response wire-shaping: OpenAI-compatible HTTP framing.

Where this sits in the architecture (mirrors providers/ and callers/):

    providers/http, providers/cli  -> UPSTREAM input concern:
                                       how do we ask a given vendor for
                                       a completion, and how do we read
                                       its reply back into our internal
                                       dict shape.

    callers/                       -> TRANSPORT concern:
                                       how do we physically dispatch a
                                       call (raw HTTP socket vs a CLI
                                       subprocess).

    wire_format.py (this file)     -> DOWNSTREAM output concern:
                                       once race_proxy_core has a final,
                                       complete response dict, how do we
                                       put it back on the wire to
                                       WHOEVER is calling this proxy, in
                                       whatever wire shape THEY asked for
                                       (plain JSON, or SSE streaming).

race_proxy_core.py must stay a neutral racing engine: it decides which
backend wins, not how the winning answer gets serialized back out. Any
knowledge of a specific consumer's request/response conventions belongs
here, never inline in do_POST — the same reasoning that keeps vendor
quirks out of race_proxy_core.py and confined to providers/*, and CLI
transport mechanics confined to callers/*.

This module knows nothing about any particular consumer by name. It
implements the OpenAI chat.completions / chat.completions.chunk wire
formats generically, per the public spec:
    https://platform.openai.com/docs/api-reference/chat/streaming
Any OpenAI-SDK-compatible client benefits from this, not one in
particular. If a consumer's own internal implementation details ever
need code review (already covered as part of a specific fix), that
review belongs in comments here, not in race_proxy_core.py.
"""
from __future__ import annotations

import json
from typing import Any, Optional


def wants_streaming_response(payload: dict) -> bool:
    """True when the inbound request asked for ``stream: true``."""
    return bool(payload.get("stream"))


def build_sse_body(data: dict) -> bytes:
    """Wrap a complete chat.completion dict as ONE SSE data-frame chunk,
    followed by ``[DONE]``, per the OpenAI chat.completions.chunk shape.

    This proxy is not an incremental streamer: it always waits for a
    full race to resolve before it has any answer at all (see
    ``race()`` in race_proxy_core.py), so there is nothing to stream
    token-by-token. What this produces is a single, complete-content
    delta chunk — a valid degenerate case of SSE streaming (one frame
    instead of many), which is exactly what makes it safe: any
    spec-compliant OpenAI streaming consumer accumulates content chunk
    by chunk regardless of how many chunks arrive, one full chunk
    included.

    Carries every field a spec-compliant consumer's stream accumulator
    is entitled to read: ``id``, ``model``, ``choices[0].delta.content``,
    ``choices[0].delta.tool_calls``, ``choices[0].finish_reason``, and
    top-level ``usage`` (only present when the winning backend or
    caller-side request asked for ``stream_options.include_usage``).
    """
    try:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        finish_reason = choice.get("finish_reason")
        tool_calls = message.get("tool_calls")
    except Exception:
        content, finish_reason, tool_calls = None, None, None

    delta: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        delta["tool_calls"] = tool_calls

    chunk: dict[str, Any] = {
        "id": data.get("id", ""),
        "object": "chat.completion.chunk",
        "created": data.get("created"),
        "model": data.get("model"),
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    if data.get("usage"):
        chunk["usage"] = data["usage"]
    # Non-standard observability tag; harmless extra field on an
    # OpenAI-shaped chunk, same as it is on the non-streaming body.
    if "_race_proxy" in data:
        chunk["_race_proxy"] = data["_race_proxy"]

    return (
        f"data: {json.dumps(chunk)}\n\n"
        f"data: [DONE]\n\n"
    ).encode()
