#!/usr/bin/env python3
"""
hermes-race-proxy: callers (Strategy — HOW to reach a backend)
=================================================================

Orthogonal to ``response_contracts.py``. A contract decides "was this
response usable" AFTER bytes came back; a :class:`Caller` decides HOW
those bytes get fetched in the first place. Keeping them separate
matters concretely for the devpass case: a CLI-only vendor needs a
completely different fetch mechanism (subprocess, not HTTP) but its
response bytes, once captured, go through the exact same contract
pipeline as an HTTP backend's — the contract layer never needs to know
or care which caller produced the bytes it's parsing.

This is Strategy, not Adapter: a Strategy swaps interchangeable
ALGORITHMS for doing the same job (fetch bytes) behind one interface,
selected by the caller; an Adapter translates one specific external
shape into an internal one after the fact. ``response_contracts.py``'s
own docstring covers why THAT layer is Adapter — this file is the
other, unrelated axis: HTTP vs subprocess vs (eventually) whatever
else a future backend needs, chosen per-backend in config, never
touching ``race()`` or the repair ladder either way.

Contract
--------
A :class:`Caller` takes a JSON request body (as bytes, headers already
merged with the caller's own transport needs) and a timeout, returns
``(status, raw_bytes)`` — the SAME two-tuple shape
``connection_pool.pooled_request`` already returned, so
:class:`~race_proxy_core.Backend` doesn't need branching logic for
"is this an HTTP backend or a CLI backend," it just calls
``self.caller.call(...)`` and gets the same shape back either way.

``status`` is an HTTP-style integer even for non-HTTP callers (200 for
success, a 5xx-shaped synthetic code for failure) — this is what lets
``response_contracts.py`` and ``repairs.py`` keep treating every
backend identically regardless of transport; a CLI caller that invents
its own status vocabulary would leak transport-specific knowledge back
into the contract layer this split is meant to keep out.
"""
from __future__ import annotations

from typing import Optional


class Caller:
    """One transport mechanism for reaching a backend. Subclass per
    transport (HTTP, CLI subprocess, ...), not per vendor — a vendor's
    identity (base_url, model, headers) lives in ``providers/``, not
    here. Most backends use :class:`HttpCaller`; only a genuinely
    CLI-only vendor (no HTTP API at all — the devpass case) needs
    :class:`CliCaller` or a new caller of your own.
    """

    def call(self, payload_bytes: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
        """Fetch a response for *payload_bytes* (already-serialized JSON
        request body). Returns ``(status, raw_bytes)`` — see module
        docstring for why *status* is always HTTP-shaped regardless of
        transport. Raises on a transport-level failure the caller
        cannot itself turn into a status code (network unreachable,
        subprocess not found, etc.) — mirrors ``pooled_request``'s
        existing raise-on-failure contract, so ``Backend._do_request``'s
        existing ``except Exception`` handling needs no changes.
        """
        raise NotImplementedError
