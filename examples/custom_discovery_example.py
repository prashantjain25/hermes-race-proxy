#!/usr/bin/env python3
"""
Example: exhaustive-probe backend discovery, real policy implementation
=========================================================================

A COMPLETE, RUNNABLE template for the discovery extension point
(discovery.py). This is NOT core hermes-race-proxy behavior, it's one
user's policy, kept as an example precisely because the ranking rules
here (which providers to trust, how many slots, what "performant"
means) are personal decisions that don't belong hardcoded into the repo
everyone installs.

The policy this file implements:

  - 2 fixed slots, always present: opencode.ai/zen's nemotron-3.5-lightning-free
    and laguna-s-2.1-free. These are known-quantity, already-tuned-in
    backends (see repairs.py for the structured-output/token-starvation
    repairs written specifically around opencode.ai/zen's failure
    shapes) that should always race regardless of what else is available.
  - 2 discovered slots: at proxy startup, exhaustively probe NVIDIA's
    build.nvidia.com catalog (GET /v1/models for the full list, then a
    real lightweight chat-completion call against each candidate) and
    keep the 2 fastest, successfully-responding models.

Wire it up:

    {
      "custom_discovery_module": "/absolute/path/to/custom_discovery_example.py",
      "nvidia_api_key": "nvapi-...",
      "backends": [ ... same static list as always, used ONLY if this
                     script fails to load or errors out, per the
                     documented fallback contract in discovery.py ... ]
    }

IMPORTANT, read before using against your own NVIDIA key:
build.nvidia.com's hosted catalog is a TRIAL service (NVIDIA's own API
Trial Terms of Service), credit-limited (starts at 1000 free credits),
rate-limited around ~40 RPM account-wide and undocumented/unstable, and
explicitly NOT licensed for production traffic per NVIDIA's FAQ
("Production use... requires NVIDIA AI Enterprise"). Treat the 2
NVIDIA-discovered slots as a best-effort supplementary fallback, not a
guaranteed-available backend, this is exactly why they're 2 of 4 slots
racing alongside 2 fixed, more predictable ones, not the only two
backends configured.

Run this file directly for a standalone demo of just the probing/ranking
logic (no proxy needed), see the __main__ block at the bottom.
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import List

# Make race_proxy_core.py / discovery.py importable when this file runs
# from examples/ directly. Not needed once these are on your PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from race_proxy_core import Backend
from discovery import probe_endpoint

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODELS_URL = f"{NVIDIA_BASE_URL}/models"

# The two always-included opencode.ai/zen backends. Headers match the
# ones already tuned for this vendor elsewhere in the repo (see
# race_proxy.local.yaml in the README), some free tiers care about a
# real-looking User-Agent / Referer.
FIXED_BACKENDS_CONFIG = [
    {
        "name": "nemotron",
        "base_url": "https://opencode.ai/zen/v1",
        "model": "nemotron-3.5-lightning-free",
        "api_key": "",
        "headers": {
            "HTTP-Referer": "https://hermes-agent.nousresearch.com",
            "X-Title": "Hermes Agent",
            "User-Agent": "HermesAgent/0.20.6",
        },
    },
    {
        "name": "laguna",
        "base_url": "https://opencode.ai/zen/v1",
        "model": "laguna-s-2.1-free",
        "api_key": "",
        "headers": {
            "HTTP-Referer": "https://hermes-agent.nousresearch.com",
            "X-Title": "Hermes Agent",
            "User-Agent": "HermesAgent/0.20.6",
        },
    },
]

#: How many of NVIDIA's catalog models to keep after ranking.
NVIDIA_DISCOVERED_SLOTS = 2

#: Cap on how many candidates to probe in parallel, NVIDIA's free tier
#: is ~40 RPM ACCOUNT-WIDE (shared across every model on one key), so
#: probing the entire 80+ model catalog at full concurrency on every
#: proxy startup would itself burn a meaningful chunk of that budget.
#: Narrow CANDIDATE_MODEL_PREFIXES below to the model families you
#: actually care about racing, rather than raising this.
MAX_CANDIDATES_TO_PROBE = 12

#: Only probe models whose ID starts with one of these, keeps the
#: exhaustive scan scoped to families you'd actually want racing
#: (fast/cheap general chat models), not e.g. embedding-only or
#: vision-only models that would never usefully answer a
#: chat-completions title/summarization prompt.
CANDIDATE_MODEL_PREFIXES = ("deepseek-ai/", "nvidia/", "meta/", "qwen/")


def _list_nvidia_models(api_key: str, timeout: float = 15.0) -> List[str]:
    """Real GET against NVIDIA's OpenAI-compatible /v1/models listing.

    Returns model IDs matching CANDIDATE_MODEL_PREFIXES, capped at
    MAX_CANDIDATES_TO_PROBE. Raises on a hard failure (bad key, network
    down), the caller (discover_backends) is expected to let that
    propagate so the documented discovery.py fallback-to-static-config
    contract kicks in.
    """
    req = urllib.request.Request(
        NVIDIA_MODELS_URL, headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    ids = [m["id"] for m in data.get("data", [])]
    candidates = [m for m in ids if m.startswith(CANDIDATE_MODEL_PREFIXES)]
    return candidates[:MAX_CANDIDATES_TO_PROBE]


def _rank_nvidia_candidates(api_key: str, candidates: List[str]) -> List[str]:
    """Probe every candidate model in PARALLEL (this is the "exhaustive
    check first" step) and return model IDs ordered fastest-first,
    successes only.

    Uses discovery.probe_endpoint, the same generic real-HTTP-call
    helper any custom discovery script can reuse, not vendor-specific
    code duplicated here.
    """
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates) or 1) as ex:
        futures = {
            ex.submit(
                probe_endpoint,
                base_url=NVIDIA_BASE_URL,
                model=model_id,
                api_key=api_key,
                timeout=20.0,
            ): model_id
            for model_id in candidates
        }
        for fut in concurrent.futures.as_completed(futures):
            model_id = futures[fut]
            results[model_id] = fut.result()

    successful = [
        (model_id, r["latency"]) for model_id, r in results.items() if r["ok"]
    ]
    successful.sort(key=lambda pair: pair[1])  # fastest first
    return [model_id for model_id, _latency in successful]


def discover_backends(cfg: dict) -> List[Backend]:
    """The entry point discovery.py's loader looks for.

    Builds the 2 fixed opencode.ai/zen backends unconditionally, then
    exhaustively probes NVIDIA's catalog and adds the top
    NVIDIA_DISCOVERED_SLOTS fastest, successfully-responding models,
    for a total of up to 4 backends racing. If NVIDIA probing fails
    entirely (bad/missing key, network down, no candidates respond),
    this still returns the 2 fixed backends rather than raising, a
    degraded 2-way race is better than no proxy at all, and the
    documented discovery.py contract only falls back to the STATIC
    `backends:` config on a raised exception, not a partial result.
    """
    backends: List[Backend] = [Backend(**entry) for entry in FIXED_BACKENDS_CONFIG]

    nvidia_key = cfg.get("nvidia_api_key", "")
    if not nvidia_key:
        print(
            "custom_discovery_example: no nvidia_api_key in config, "
            "skipping NVIDIA discovery, using the 2 fixed backends only",
            file=sys.stderr,
        )
        return backends

    try:
        candidates = _list_nvidia_models(nvidia_key)
        ranked = _rank_nvidia_candidates(nvidia_key, candidates)
    except Exception as e:
        print(
            f"custom_discovery_example: NVIDIA discovery failed ({e}), "
            f"using the 2 fixed backends only",
            file=sys.stderr,
        )
        return backends

    for model_id in ranked[:NVIDIA_DISCOVERED_SLOTS]:
        backends.append(Backend(
            name=f"nvidia-{model_id.split('/')[-1]}",
            base_url=NVIDIA_BASE_URL,
            model=model_id,
            api_key=nvidia_key,
        ))

    return backends


if __name__ == "__main__":
    # Standalone demo: run just the discovery/ranking step and print the
    # result, without starting a proxy. Needs a real NVIDIA key passed
    # as the first CLI arg (never hardcode a key into this file).
    if len(sys.argv) < 2:
        print("Usage: python3 custom_discovery_example.py <nvidia_api_key>", file=sys.stderr)
        sys.exit(1)
    demo_cfg = {"nvidia_api_key": sys.argv[1]}
    result = discover_backends(demo_cfg)
    print(f"Discovered {len(result)} backend(s):")
    for b in result:
        print(f"  {b.name}: {b.base_url} / {b.model}")
