#!/usr/bin/env python3
"""
Example: N x M provider pool as a real discovery policy
============================================================

The rewrite of ``custom_discovery_example.py`` on top of the
``providers/`` package. Same policy as before (2 fixed opencode.ai/zen
backends + top-2 from an exhaustive NVIDIA catalog probe), now
expressed as a declarative list of :class:`~providers.pool.ProviderSlot`
instead of hand-rolled opencode.ai/zen-specific + NVIDIA-specific code
side by side. Adding a THIRD provider (OpenRouter, DeepInfra, or your
own) is now one more ``ProviderSlot`` entry, not new bespoke logic.

Wire it up exactly like the old example:

    {
      "custom_discovery_module": "/absolute/path/to/provider_pool_example.py",
      "nvidia_api_key": "nvapi-...",
      "openrouter_api_key": "",
      "deepinfra_api_key": "",
      "backends": [ ... unchanged static fallback list ... ]
    }

Only ``nvidia_api_key`` needs to be set for this exact policy — the
OpenRouter/DeepInfra config keys are read and passed through but their
slots are commented out below by default (both REQUIRE a paid/keyed
account for actual chat completions per providers/openrouter.py and
providers/deepinfra.py's docstrings; uncomment and supply a real key to
enable either).

IMPORTANT: build.nvidia.com is a credit-limited TRIAL service, not a
stable free tier — see providers/nvidia_build.py's docstring and the
README's "Things I haven't solved yet" section before relying on it for
anything beyond best-effort supplementary racing.

Run this file directly for a standalone demo (no proxy needed):

    python3 examples/provider_pool_example.py <nvidia_api_key>
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make providers/, race_proxy_core.py importable when run directly from
# examples/. Not needed once these are on your PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.opencode_zen import OpenCodeZenProvider
from providers.nvidia_build import NvidiaBuildProvider
from providers.pool import ProviderSlot, build_pool


def discover_backends(cfg: dict):
    """The entry point discovery.py's loader looks for."""
    slots = [
        # Fixed: always race these two exact opencode.ai/zen models —
        # no probing needed, no key needed (keyless free tier).
        ProviderSlot(
            provider=OpenCodeZenProvider(),
            model_ids=["nemotron-3.5-lightning-free", "laguna-s-2.1-free"],
        ),
        # Discovered: exhaustively probe NVIDIA's catalog, keep the top
        # 2 fastest successfully-responding models. Scoped to a handful
        # of prefixes so the exhaustive probe doesn't burn the ~40 RPM
        # account-wide trial budget on the full 80+ model catalog.
        ProviderSlot(
            provider=NvidiaBuildProvider(),
            api_key=cfg.get("nvidia_api_key", ""),
            top_n=2,
            candidate_prefixes=("deepseek-ai/", "nvidia/", "meta/", "qwen/"),
            max_candidates=12,
        ),
        # Add a third provider here the same way, e.g.:
        # ProviderSlot(
        #     provider=OpenRouterProvider(),
        #     api_key=cfg.get("openrouter_api_key", ""),
        #     top_n=2,
        #     candidate_prefixes=(),  # or narrow to specific vendors
        # ),
    ]
    return build_pool(slots)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 provider_pool_example.py <nvidia_api_key>", file=sys.stderr)
        sys.exit(1)
    demo_cfg = {"nvidia_api_key": sys.argv[1]}
    result = discover_backends(demo_cfg)
    print(f"Discovered {len(result)} backend(s):")
    for b in result:
        print(f"  {b.name}: {b.base_url} / {b.model}")
