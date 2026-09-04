#!/usr/bin/env python3
"""
opencode.ai/zen provider, keyless free-tier models.

Verified live (Aug 2026):
- base_url https://opencode.ai/zen/v1, OpenAI-compatible
  /chat/completions and /models endpoints, both confirmed working
  keyless.
- `-free` suffixed models (nemotron-3.5-lightning-free,
  nemotron-3.5-lightning-free, laguna-s-2.1-free, etc.) require NO
  Authorization header at all, sending an empty Bearer token is safe,
  but some of this vendor's free models 401 on ANY recognized bearer
  token shape, so requires_api_key is False and no Authorization
  header is sent unless the caller explicitly supplies one.
- Known unreliable models seen in live testing: hy3-free (reported
  deprecated/removed in multiple GitHub issues, inconsistent live),
  deepseek-v4-flash-free and muse-spark-1.2-contributor-free (observed
  intermittent 400/500 "Endpoint is unavailable" during this session).
  Not filtered out here, rank_models()'s probe step naturally drops
  whatever isn't responding at discovery time.

File named after the vendor (opencode.ai/zen), not after any one
model it hosts, since one base_url/auth contract serves every model
below. The build_*_backend() helpers exist so a caller who wants one
specific named model doesn't need to remember its exact model-id
string; they all delegate to the same OpenCodeZenProvider underneath,
confirmed against opencode.ai/zen's live /v1/models catalog
(Sep 2026): nemotron-3.5-lightning-free, laguna-s-2.1-free,
glm-5.2, kimi-k3.
"""
from __future__ import annotations

from typing import List, Optional

from providers.base import Provider


class OpenCodeZenProvider(Provider):
    name = "opencode-zen"
    base_url = "https://opencode.ai/zen/v1"
    requires_api_key = False

    def default_headers(self) -> dict:
        # Some opencode.ai/zen free-tier calls behave better with a
        # plausible User-Agent/Referer pair, carried over from the
        # config this repo already ships in examples/race_proxy.example.yaml.
        return {
            "HTTP-Referer": "https://github.com/prashantjain25/hermes-race-proxy",
            "X-Title": "hermes-race-proxy",
        }

    def list_models(self, api_key: str = "", timeout: float = 15.0) -> List[str]:
        """Only the ``-free`` suffixed models, this provider's catalog
        also lists paid models, which would fail with requires_api_key
        left False and no key supplied. Filter here rather than at the
        pool layer so ``list_models()`` alone is a trustworthy "what can
        I actually call for free" answer.
        """
        all_models = super().list_models(api_key, timeout)
        return [m for m in all_models if m.endswith("-free")]


#: Model IDs this file has named helpers for, confirmed present in
#: opencode.ai/zen's live /v1/models catalog (Sep 2026). glm-5.2 and
#: kimi-k3 are NOT ``-free`` suffixed (paid models on this vendor),
#: an api_key is required for those two, unlike nemotron/laguna.
NEMOTRON_MODEL_ID = "nemotron-3.5-lightning-free"
LAGUNA_MODEL_ID = "laguna-s-2.1-free"
GLM_MODEL_ID = "glm-5.2"
KIMI_MODEL_ID = "kimi-k3"


def build_nemotron_backend(name: str = "nemotron"):
    """Keyless free-tier Nemotron on opencode.ai/zen."""
    return OpenCodeZenProvider().build_backend(NEMOTRON_MODEL_ID, name=name)


def build_laguna_backend(name: str = "laguna"):
    """Keyless free-tier Laguna on opencode.ai/zen."""
    return OpenCodeZenProvider().build_backend(LAGUNA_MODEL_ID, name=name)


def build_glm_backend(api_key: str, name: str = "glm"):
    """GLM 5.2 on opencode.ai/zen. Paid model, needs a real api_key
    (not ``-free`` suffixed, requires_api_key semantics don't apply
    the way they do for nemotron/laguna above)."""
    return OpenCodeZenProvider().build_backend(GLM_MODEL_ID, api_key=api_key, name=name)


def build_kimi_backend(api_key: str, name: str = "kimi"):
    """Kimi K3 on opencode.ai/zen. Paid model, needs a real api_key."""
    return OpenCodeZenProvider().build_backend(KIMI_MODEL_ID, api_key=api_key, name=name)
