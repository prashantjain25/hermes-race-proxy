#!/usr/bin/env python3
"""
OpenRouter provider.

Verified live (Aug 2026):
- base_url https://openrouter.ai/api/v1 — /models listing works
  keyless (confirmed 396 models returned with no Authorization header).
- /chat/completions REQUIRES a valid API key — confirmed live: a
  keyless call returns HTTP 401 "No cookie auth credentials found",
  even against a `:free`-suffixed model. requires_api_key = True.
- Free-tier models on this vendor are identified by an explicit
  `:free` suffix on the model ID (e.g.
  "inclusionai/ling-3.0-flash-fin:free") — confirmed present in the
  live /models listing (8+ found in a single page during this
  session's check). Still needs a key to actually call, per above.
- OpenRouter's own docs recommend HTTP-Referer and X-Title headers for
  attribution/rankings — included here as a courtesy default, not
  required for the API to function.
"""
from __future__ import annotations

from typing import List

from providers.base import Provider


class OpenRouterProvider(Provider):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    requires_api_key = True

    def default_headers(self) -> dict:
        return {
            "HTTP-Referer": "https://github.com/prashantjain25/hermes-race-proxy",
            "X-Title": "hermes-race-proxy",
        }

    def list_models(self, api_key: str = "", timeout: float = 15.0) -> List[str]:
        """The full listing works without a key (confirmed live), but
        actually CALLING any of these models needs one — see the class
        docstring. ``only_free`` narrows to ``:free``-suffixed IDs,
        matching this vendor's own free-tier naming convention.
        """
        all_models = super().list_models(api_key, timeout)
        return all_models

    def list_free_models(self, api_key: str = "", timeout: float = 15.0) -> List[str]:
        """Convenience: only the ``:free``-suffixed subset of
        :meth:`list_models`."""
        return [m for m in self.list_models(api_key, timeout) if m.endswith(":free")]
