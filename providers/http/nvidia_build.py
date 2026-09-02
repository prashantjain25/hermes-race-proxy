#!/usr/bin/env python3
"""
NVIDIA build.nvidia.com provider.

Verified live (Aug 2026):
- base_url https://integrate.api.nvidia.com/v1, /models listing
  and /chat/completions BOTH confirmed working with a real trial key
  during this session (200 OK responses, 83 models listed, correct
  structured-output handling on the exact strict-json_schema payload
  that opencode.ai/zen's free models reject).
- requires_api_key = True. NVIDIA's own build.nvidia.com FAQ and API
  Trial Terms of Service confirm this is a CREDIT-LIMITED TRIAL
  service, not a stable free tier:
    - Sign-up grants 1000 free API credits (5000 total available on
      request), not unlimited/recurring free usage.
    - Community-observed rate limit ~40 RPM, ACCOUNT-WIDE (shared
      across every model called with one key, not per-model),
      unpublished, no official SLA, "dependent on model, use-case and
      current overall traffic" per NVIDIA staff on their developer
      forum.
    - Explicitly barred from production use per NVIDIA's own FAQ:
      "Production use... requires NVIDIA AI Enterprise."
  Treat any backend built from this provider as a best-effort
  supplementary racer alongside more predictable providers, never as
  the only backend configured, see the README's "Things I haven't
  solved yet" section.
"""
from __future__ import annotations

from providers.base import Provider


class NvidiaBuildProvider(Provider):
    name = "nvidia-build"
    base_url = "https://integrate.api.nvidia.com/v1"
    requires_api_key = True
