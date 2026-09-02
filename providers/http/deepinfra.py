#!/usr/bin/env python3
"""
DeepInfra provider.

Verified live (Aug 2026):
- base_url https://api.deepinfra.com/v1/openai, OpenAI-compatible
  /models listing confirmed working keyless (190 models returned).
- /chat/completions REQUIRES an API key, confirmed live: a keyless
  call returns HTTP 401 {"error": {"message": "missing API key", ...,
  "code": "invalid_api_key"}}. requires_api_key = True.
- No free-tier model naming convention observed in the listing (no
  `:free`/`-free` suffix pattern like OpenRouter/opencode.ai/zen use):
  DeepInfra bills per-token on every model; there is no keyless or
  credit-free path confirmed for this vendor. Included for
  completeness (users with their own DeepInfra key can still use it
  as a pool member), but list_models() has no "free subset" helper
  the way opencode/openrouter do, because there isn't one.
"""
from __future__ import annotations

from providers.base import Provider


class DeepInfraProvider(Provider):
    name = "deepinfra"
    base_url = "https://api.deepinfra.com/v1/openai"
    requires_api_key = True
