#!/usr/bin/env python3
"""
opencode.ai/zen provider — keyless free-tier models.

Verified live (Aug 2026):
- base_url https://opencode.ai/zen/v1, OpenAI-compatible
  /chat/completions and /models endpoints, both confirmed working
  keyless.
- `-free` suffixed models (ling-3.0-flash-fin-free,
  nemotron-3.5-lightning-free, laguna-s-2.1-free, etc.) require NO
  Authorization header at all — sending an empty Bearer token is safe,
  but some of this vendor's free models 401 on ANY recognized bearer
  token shape, so requires_api_key is False and no Authorization
  header is sent unless the caller explicitly supplies one.
- Known unreliable models seen in live testing: hy3-free (reported
  deprecated/removed in multiple GitHub issues, inconsistent live),
  deepseek-v4-flash-free and muse-spark-1.2-contributor-free (observed
  intermittent 400/500 "Endpoint is unavailable" during this session).
  Not filtered out here — rank_models()'s probe step naturally drops
  whatever isn't responding at discovery time.
"""
from __future__ import annotations

from typing import List

from providers.base import Provider


class OpenCodeZenProvider(Provider):
    name = "opencode-zen"
    base_url = "https://opencode.ai/zen/v1"
    requires_api_key = False

    def default_headers(self) -> dict:
        # Some opencode.ai/zen free-tier calls behave better with a
        # plausible User-Agent/Referer pair — carried over from the
        # config this repo already ships in race_proxy.example.json.
        return {
            "HTTP-Referer": "https://github.com/prashantjain25/hermes-race-proxy",
            "X-Title": "hermes-race-proxy",
        }

    def list_models(self, api_key: str = "", timeout: float = 15.0) -> List[str]:
        """Only the ``-free`` suffixed models — this provider's catalog
        also lists paid models, which would fail with requires_api_key
        left False and no key supplied. Filter here rather than at the
        pool layer so ``list_models()`` alone is a trustworthy "what can
        I actually call for free" answer.
        """
        all_models = super().list_models(api_key, timeout)
        return [m for m in all_models if m.endswith("-free")]
