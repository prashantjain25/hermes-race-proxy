#!/usr/bin/env python3
"""
hermes-race-proxy: provider pool
====================================

The N×M layer: given a set of enabled providers (each with its own
connection contract, see ``providers/base.py`` and the concrete
provider modules) and how many models per provider you want racing,
build one flat list of :class:`~race_proxy_core.Backend` instances,
the union of every enabled provider's own top-N ranked models.

    total backends = sum(models_per_provider for each enabled provider)

This is the layer ``discovery.py``'s ``discover_backends(cfg)`` calls
into for a policy like "always include these fixed backends, plus the
top 2 from each of these 3 providers", see
``examples/provider_pool_example.py`` for that wired up as a complete,
runnable discovery script.

Nothing here is imported by ``race_proxy_core.py``, this whole package
is an OPTIONAL convenience layer for building your own discovery
policy, same as ``examples/custom_discovery_example.py`` was before
this refactor (that file's opencode.ai/zen + NVIDIA logic is now
expressed via this package instead of ad-hoc inline code, see
``examples/provider_pool_example.py`` for the equivalent, rewritten on
top of ``providers/``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from providers.base import Provider, rank_models

logger = logging.getLogger("race_proxy.providers.pool")


@dataclass
class ProviderSlot:
    """One entry in a pool spec: a provider, its credentials, and how
    many of its models should end up racing.

    ``api_key=""`` is valid and meaningful for keyless providers (e.g.
    ``OpenCodeZenProvider``, whose free models need no key at all),
    the pool builder does not require every slot to have a key, only
    that ``provider.requires_api_key`` slots that lack one are skipped
    with a clear log line rather than silently producing zero backends.

    ``model_ids``: if given, these EXACT models are used verbatim (no
    discovery/ranking probe), the "fixed backends" half of an N×M
    policy, e.g. "always include nemotron-3.5-lightning-free and
    laguna-s-2.1-free from opencode-zen." If omitted, ``top_n`` models
    are chosen by :func:`build_pool`'s exhaustive-probe-and-rank step
    instead, the "discovered" half.
    """
    provider: Provider
    api_key: str = ""
    top_n: int = 2
    model_ids: Optional[List[str]] = None
    #: Optional filter narrowing this provider's full catalog before
    #: ranking, e.g. only "deepseek-ai/", "meta/" prefixed IDs, so an
    #: exhaustive probe doesn't waste calls on embedding/vision-only
    #: models that would never usefully answer a chat prompt.
    candidate_prefixes: tuple = field(default_factory=tuple)
    #: Cap on how many candidates get probed for ranking, independent
    #: of top_n, keeps an exhaustive scan bounded even against a
    #: provider with hundreds of models in its catalog.
    max_candidates: int = 12


def build_pool(slots: List[ProviderSlot], probe_timeout: float = 20.0) -> list:
    """Build the flat N×M backend list from *slots*.

    For each slot:
    - If ``requires_api_key`` and no key was given, skip it (logged),
      not raise, one misconfigured provider shouldn't take down the
      whole pool when others are fine.
    - If ``model_ids`` is set, build backends for exactly those models,
      no probing.
    - Otherwise, list the provider's catalog (optionally filtered by
      ``candidate_prefixes``, capped at ``max_candidates``), probe them
      all in parallel via :func:`providers.base.rank_models`, and keep
      the fastest ``top_n`` successful responders.

    Returns the concatenation of every slot's backends, in slot order.
    A slot that fails entirely (listing/probing raised, or nothing
    responded) contributes zero backends and logs a warning rather than
    aborting the whole pool, same "degrade, don't collapse" contract
    ``discovery.py`` documents at the top level.
    """
    from race_proxy_core import Backend  # local import, see providers/base.py's note

    all_backends: List[Backend] = []
    for slot in slots:
        p = slot.provider
        if p.requires_api_key and not slot.api_key:
            logger.warning(
                "Skipping provider '%s': requires_api_key is True but no "
                "api_key was given in its ProviderSlot", p.name,
            )
            continue

        if slot.model_ids is not None:
            for model_id in slot.model_ids:
                all_backends.append(p.build_backend(model_id, slot.api_key))
            continue

        try:
            catalog = p.list_models(slot.api_key)
        except Exception:
            logger.warning("Provider '%s': failed to list models", p.name, exc_info=True)
            continue

        if slot.candidate_prefixes:
            catalog = [m for m in catalog if m.startswith(slot.candidate_prefixes)]
        candidates = catalog[: slot.max_candidates]
        if not candidates:
            logger.warning(
                "Provider '%s': no candidate models to probe (catalog=%d, "
                "prefixes=%s)", p.name, len(catalog), slot.candidate_prefixes,
            )
            continue

        try:
            ranked = rank_models(p, candidates, slot.api_key, timeout=probe_timeout)
        except Exception:
            logger.warning("Provider '%s': ranking probe failed", p.name, exc_info=True)
            continue

        chosen = ranked[: slot.top_n]
        if not chosen:
            logger.warning(
                "Provider '%s': none of %d probed candidates responded successfully",
                p.name, len(candidates),
            )
            continue
        for model_id in chosen:
            all_backends.append(p.build_backend(model_id, slot.api_key))

    logger.info(
        "Provider pool built: %d backend(s) from %d slot(s): %s",
        len(all_backends), len(slots), ", ".join(b.name for b in all_backends),
    )
    return all_backends
