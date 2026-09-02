#!/usr/bin/env python3
"""
gcp.py, Google Gemini provider, via its OpenAI-compatible endpoint.

base_url: https://generativelanguage.googleapis.com/v1beta/openai
This is Google's own OpenAI-compatibility shim for the Gemini API
(GCP/AI Studio), not a third-party aggregator, hence the file name:
one file per BACKING PLATFORM, matching opencode.py (opencode.ai/zen)
and openrouter.py (openrouter.ai) alongside it.

Requires a real Google AI Studio / GCP API key, always, there is no
keyless tier here (unlike opencode.ai/zen's `-free` models).

Verified live in production, not a one-off check: this repo's own
compaction proxy (race_proxy.local.json) has been racing a "gemini"
backend on exactly this base_url/model since before this file existed,
and /Users/dolphin/.hermes/logs/race-proxy.log shows it winning races
repeatedly with real 200s and real response bytes (status=200,
resp_bytes in the hundreds to tens of thousands, latency 0.8s-14s
across many separate race-done ok=True entries). That's the
production model, gemini-3.5-flash-lite, confirmed via that config
file and that log, not a guess.

An earlier check in this session tried "gemini-3.6-flash" instead
(picked from a 404 error message's own suggested-replacement text for
a different, wrong model ID), got one real 200 back confirming the
endpoint/auth contract, but "gemini-3.6-flash" is NOT what's actually
run in production here and should not be assumed correct without
re-checking; gemini-3.5-flash-lite is the one with a real, repeated,
current production track record and is what GEMINI_FLASH_MODEL_ID
below points at.

This endpoint can return HTTP 503 "high demand" under load, purely
Google-side capacity, not an integration issue; retry logic already
lives in this proxy's core racing engine (race_proxy_core.py's
Backend.call) for exactly that case.

list_models() (this provider's `/models` catalog listing) was not
exercised live in this check, only chat/completions has direct
evidence, both from the ad-hoc check and from production log history.
"""
from __future__ import annotations

from providers.base import Provider


class GcpGeminiProvider(Provider):
    name = "gcp-gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    requires_api_key = True


#: Model ID with a real, repeated production track record: this
#: repo's own compaction proxy has been racing this exact model on
#: this exact base_url and winning, confirmed via
#: race_proxy.local.json and /Users/dolphin/.hermes/logs/race-proxy.log
#: (many status=200 race-done ok=True entries), not a one-off check.
GEMINI_FLASH_MODEL_ID = "gemini-3.5-flash-lite"


def build_gemini_backend(api_key: str, name: str = "gemini", model_id: str = GEMINI_FLASH_MODEL_ID):
    """Gemini on its own OpenAI-compat endpoint. Always needs a real
    api_key, see this module's docstring, there is no keyless tier.
    """
    return GcpGeminiProvider().build_backend(model_id, api_key=api_key, name=name)
