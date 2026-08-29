#!/usr/bin/env python3
"""
hermes-race-proxy: repair strategies
=====================================

Pluggable retry logic for backend responses that failed or came back
unusable in a *fixable* way, a 400 that only names an opaque wrapper
message, a 200 with empty content because a reasoning model burned its
whole token budget thinking, or whatever the next vendor invents.

This module is intentionally separate from ``race_proxy_core.py``. The
core module knows how to make an HTTP request and how to run a race; it
has ZERO knowledge of *why* a response might be broken or *how* to fix
it. All of that lives here, behind one small interface
(:class:`RepairStrategy`), so you can add support for a new vendor's
failure shape by writing a new class in your OWN file and registering
it, no editing of race_proxy_core.py, no new if/elif branch in someone
else's request-handling code, and nothing that a `git pull` on the core
project can ever conflict with.

Quick start: bring your own model's fix
-----------------------------------------
1. Subclass :class:`RepairStrategy` (see the two built-ins below for the
   shape).
2. In your own file (anywhere, doesn't need to live in this repo), write:

    from repairs import RepairStrategy, RepairRegistry

    class MyVendorQuirk(RepairStrategy):
        name = "my_vendor_quirk"
        max_rungs = 1

        def applies(self, body, result):
            return not result.get("ok") and "some vendor-specific text" in (result.get("error") or "")

        def propose(self, body, result, rung):
            new_body = dict(body)
            new_body.pop("some_field_this_vendor_hates", None)
            return new_body

    def register(registry: RepairRegistry) -> None:
        registry.register(MyVendorQuirk())

3. Point the proxy at it, either via config:

       custom_repairs_module: /path/to/my_repairs.py

   or programmatically if you're importing this as a library:

       from repairs import DEFAULT_REGISTRY
       DEFAULT_REGISTRY.register(MyVendorQuirk())

Your strategy now runs for every backend that opts into it (backends
opt into ALL registered strategies by default, see ``repairs:`` in a
backend's config entry to restrict to a subset).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable, List, Optional

logger = logging.getLogger("race_proxy.repairs")


# ── The extension contract ──────────────────────────────────────────────

class RepairStrategy:
    """One fixable-failure pattern and how to retry around it.

    A strategy owns a small state machine expressed as numbered "rungs"
    (1, 2, 3, ...), each rung is one alternate request shape to try, in
    order, until either one resolves the failure or the rungs run out.
    Most strategies need only one rung; :class:`StructuredOutputRelaxation`
    below uses two to show the pattern for a multi-step ladder.

    Override ``applies``, ``propose``, and optionally ``resolved``.
    Do not override anything else, the engine that drives instances of
    this class lives in :class:`RepairRegistry` / ``race_proxy_core.py``,
    and treats every strategy identically regardless of what it does
    internally.
    """

    #: Short, unique, log-friendly identifier. Shows up in the response's
    #: ``_race_proxy.repaired_rung`` tag as ``"<name>:<rung>"``.
    name: str = "base"

    #: How many rungs (alternate request shapes) this strategy will try
    #: before giving up on the current failure.
    max_rungs: int = 1

    def applies(self, body: dict, result: dict) -> bool:
        """Fast pre-check: could this strategy plausibly fix *result*?

        *body* is the request that produced *result* (the most recent
        attempt, not necessarily the original: a body already modified
        by an earlier strategy in the chain is passed through). *result*
        is this backend's ``{"ok", "status_code", "data", "error", ...}``
        dict from :meth:`Backend._do_request`.

        Return False to skip this strategy for this failure entirely
        (e.g. a strategy for 400s should return False on a 200). The
        default only fires on hard failures (``ok`` is False), override
        for strategies that repair a 200-but-unusable response, like
        :class:`TokenStarvationBoost` does.
        """
        return not result.get("ok", False)

    def propose(self, body: dict, result: dict, rung: int) -> Optional[dict]:
        """Return a modified request body to retry at *rung*, or None.

        Returning None means "this rung doesn't apply", the engine skips
        straight to the next rung (or the next strategy, if rungs are
        exhausted). This lets a strategy skip a rung that isn't relevant
        to the current body without aborting the whole ladder (see
        :class:`StructuredOutputRelaxation` rung 1, which returns None
        when there's no ``strict: true`` flag left to drop).

        Must not mutate *body* in place, always return a new dict.
        """
        raise NotImplementedError

    def resolved(self, result: dict) -> bool:
        """Return True if a retry's *result* counts as this strategy's
        problem being fixed.

        Default: plain HTTP success. Override when success needs a
        stronger check, :class:`TokenStarvationBoost` also has to
        confirm content actually came back this time, not just that the
        HTTP call itself succeeded.
        """
        return bool(result.get("ok"))


class RepairRegistry:
    """An ordered list of :class:`RepairStrategy` instances.

    Construct your own to fully control what runs (e.g. only your custom
    strategy, none of the built-ins), or extend :data:`DEFAULT_REGISTRY`.
    Registries are cheap, stateless containers, building a filtered copy
    per-backend (see :meth:`select`) is the intended pattern, since
    different backends may want different repairs enabled.
    """

    def __init__(self, strategies: Optional[Iterable[RepairStrategy]] = None):
        self.strategies: List[RepairStrategy] = list(strategies or [])

    def register(self, strategy: RepairStrategy) -> "RepairRegistry":
        """Add a strategy. Returns self so calls can be chained."""
        self.strategies.append(strategy)
        return self

    def select(self, names: Iterable[str]) -> "RepairRegistry":
        """Return a NEW registry containing only strategies whose
        ``.name`` is in *names*, preserving this registry's order.

        Used to build a per-backend registry from a shared master list,
        e.g. a backend config's ``repairs: [format]`` opts into only the
        structured-output ladder, not the token-starvation one.
        """
        wanted = set(names)
        return RepairRegistry([s for s in self.strategies if s.name in wanted])

    def max_extra_attempts(self) -> int:
        """Upper bound on retry HTTP calls this registry could make for
        one incoming request, used by the caller to divide up a shared
        overall timeout across the original attempt plus every possible
        repair rung."""
        return sum(s.max_rungs for s in self.strategies)

    def attempt(
        self,
        do_request: Callable[[dict, float], dict],
        body: dict,
        result: dict,
        deadline: float,
        backend_name: str = "",
    ) -> tuple[dict, dict]:
        """Run every registered strategy, in order, against *result*.

        Each strategy gets a chance to run its full rung ladder. A
        strategy that resolves the failure updates the working
        ``(body, result)`` pair and the engine moves on to the NEXT
        strategy, so independent repairs can stack in a single call
        (e.g. a structured-output fix followed by a token-budget fix,
        tagged ``"format:2+tokens:1"``). A strategy that never resolves
        leaves ``result`` as its own most recent (failed) attempt and the
        engine moves on regardless, later strategies still get a chance
        even if an earlier one made no progress.

        *do_request* is a ``(body, timeout) -> result`` callable the
        caller supplies (normally a closure over
        :meth:`Backend._do_request`), this module does not know how to
        make an HTTP request, only when to ask for one and how much time
        is left to give it.

        *deadline* is a ``time.monotonic()``-comparable timestamp: the
        point past which no further attempt should be started. Each
        retry gets whatever time remains until the deadline (not a fixed
        upfront slice of the total budget), some vendors (observed:
        ``nemotron-3.5-lightning-free`` at a boosted ``max_tokens``) take
        20-30s for a single legitimate call, and pre-dividing the total
        race timeout by the worst-case number of possible attempts starves
        exactly the slow-but-eventually-successful call this repair
        exists to rescue.

        Returns ``(final_result, final_body)``. ``final_result`` always
        carries a ``"repaired_rung"`` key: ``None`` if nothing was
        applied, or a ``"+"``-joined list of ``"<strategy>:<rung>"`` tags
        for every repair that actually fixed something.
        """
        applied_tags: List[str] = []
        for strategy in self.strategies:
            if not strategy.applies(body, result):
                continue
            for rung in range(1, strategy.max_rungs + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._finalize(result, applied_tags), body
                candidate = strategy.propose(body, result, rung)
                if candidate is None:
                    continue  # this rung doesn't apply; try the next one
                logger.info(
                    "%s: applying repair '%s' rung %d (%.1fs remaining)",
                    backend_name, strategy.name, rung, remaining,
                )
                retry_result = do_request(candidate, remaining)
                if strategy.resolved(retry_result):
                    result = retry_result
                    body = candidate
                    applied_tags.append(f"{strategy.name}:{rung}")
                    break  # this strategy is satisfied; next strategy
                result = retry_result
                if not strategy.applies(candidate, result):
                    break  # strategy's own precondition no longer holds
        return self._finalize(result, applied_tags), body

    @staticmethod
    def _finalize(result: dict, applied_tags: List[str]) -> dict:
        result["repaired_rung"] = "+".join(applied_tags) if applied_tags else None
        return result


# ── Shared request/response inspection helpers ──────────────────────────
# Small, strategy-agnostic pieces of logic that more than one built-in
# strategy needs. Kept private to this module, a custom strategy in your
# own file doesn't need these, but is free to import and reuse them.

def _get_message_content(data: Optional[dict]) -> tuple[bool, Optional[str], Optional[str]]:
    """Return (shape_ok, content, finish_reason) from a chat.completion body.

    ``shape_ok`` is False when *data* doesn't even look like a
    chat.completion response (missing choices/message), callers should
    treat that as "can't tell, don't touch it" rather than confusing it
    with a genuinely null/empty ``content`` field, which some vendors
    send literally as ``"content": null`` on a starved response (not
    ``""``). Conflating those two cases was a real bug during this
    module's refactor: treating ``content is None`` from a shape
    mismatch the same as ``content is None`` from a starved-but-valid
    response caused the starvation repair to silently never fire.
    """
    if not isinstance(data, dict):
        return False, None, None
    try:
        choice = data["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content")
        finish_reason = choice.get("finish_reason")
        return True, content, finish_reason
    except (KeyError, IndexError, TypeError):
        return False, None, None


# ── Built-in strategy 1: structured-output relaxation ───────────────────

class StructuredOutputRelaxation(RepairStrategy):
    """Progressively loosen ``response_format`` on a 400/422.

    Many OpenAI-compatible gateways (free-tier aggregators especially)
    advertise Chat Completions compatibility but reject strict
    JSON-Schema structured output with an opaque 400 that never mentions
    ``response_format`` by name (a generic ``"Upstream request failed:
    [400] Provider returned error"`` is a real example, from
    opencode.ai/zen). A caller can't reliably string-match its way to
    "this was a structured-output rejection" across every vendor's error
    envelope shape, so instead of trying to *detect* the cause, this
    strategy just tries looser contracts in order whenever the response
    was a 400/422 AND the request carried a ``response_format``.

    Rung 1: ``response_format.json_schema.strict`` forced to False,
             schema kept. Some vendors support json_schema mode but
             reject strict enforcement specifically.
    Rung 2: ``response_format`` stripped entirely. Schema enforcement
             degrades to whatever the system/user prompt asked for in
             plain text, the CALLER's own response parser needs a
             loose-JSON-scan fallback for this to still work (Hermes's
             ``title_generator._extract_title_text`` already has one).
    """

    name = "format"
    max_rungs = 2

    def applies(self, body: dict, result: dict) -> bool:
        if result.get("ok"):
            return False
        if result.get("status_code") not in (400, 422):
            return False
        return isinstance(body.get("response_format"), dict)

    def propose(self, body: dict, result: dict, rung: int) -> Optional[dict]:
        rf = body.get("response_format")
        if not isinstance(rf, dict):
            return None
        if rung == 1:
            if rf.get("type") != "json_schema":
                return None
            json_schema = rf.get("json_schema")
            if not isinstance(json_schema, dict) or json_schema.get("strict") is not True:
                return None  # already non-strict or no strict flag to drop
            new_body = dict(body)
            new_rf = dict(rf)
            new_json_schema = dict(json_schema)
            new_json_schema["strict"] = False
            new_rf["json_schema"] = new_json_schema
            new_body["response_format"] = new_rf
            return new_body
        if rung == 2:
            new_body = dict(body)
            new_body.pop("response_format", None)
            return new_body
        return None


# ── Built-in strategy 2: token-starvation boost ──────────────────────────

class TokenStarvationBoost(RepairStrategy):
    """Retry with a bigger ``max_tokens`` when reasoning ate the budget.

    Reasoning models (ling, nemotron, and similar) spend part of their
    completion budget on hidden ``reasoning`` tokens before ever writing
    visible ``content``. A caller that sets a small ``max_tokens`` for a
    short-answer task (Hermes's title_generator uses 64, expecting "a
    title is a handful of tokens") can starve the model in one of two
    observed shapes:

    1. **Empty content.** All budget went to the hidden ``reasoning``
       field; ``content`` is empty/whitespace, ``finish_reason`` is
       ``"length"``.
    2. **Truncated-reasoning-as-content.** The model's reasoning process
       leaks (or gets copied) into the visible ``content`` field itself,
       then gets cut off mid-thought at the token ceiling, you get a
       non-empty but garbage string like ``"Here's a thinking process:
       1. Analyze User Input..."`` with ``finish_reason: "length"``.
       Observed live from ``nemotron-3.5-lightning-free`` at
       ``max_tokens=64`` on the same title-generation payload that
       triggers shape 1 on ``ling``. Neither an empty-content check nor
       a downstream JSON parse of this content will ever produce a valid
       answer, it's not JSON, it's an unfinished thought.

    Both shapes share the same root cause (max_tokens too small for this
    model's reasoning overhead on this prompt) and the same fix (retry
    with a bigger budget), so this strategy triggers on ANY
    ``finish_reason: "length"`` result while ``max_tokens`` is below the
    safe floor, not just the empty-content case. This is deliberately
    permissive: a model that was truncated at a small max_tokens rarely
    has produced a trustworthy answer either way, so retrying with room
    to actually finish is the right move for both shapes.

    This is a model-behavior problem, not a per-caller bug: no amount
    of prompt tuning fixes it, because the model doesn't know its own
    budget is too small until it has already spent it.
    """

    name = "tokens"
    max_rungs = 1

    def __init__(self, floor: int = 2000):
        #: See references/auxiliary-compression-benchmarks.md for the
        #: benchmark data behind this default: reasoning consumed the
        #: whole budget below ~1500 tokens in production trials;
        #: 2000-4000 is the safe range.
        self.floor = floor

    def applies(self, body: dict, result: dict) -> bool:
        if not result.get("ok"):
            return False
        shape_ok, _content, finish_reason = _get_message_content(result.get("data"))
        if not shape_ok:
            return False  # not even a chat.completion shape, don't guess
        if finish_reason != "length":
            return False  # stopped naturally; a bigger budget wouldn't change anything
        current_max_tokens = body.get("max_tokens")
        return current_max_tokens is None or current_max_tokens < self.floor

    def propose(self, body: dict, result: dict, rung: int) -> Optional[dict]:
        if rung != 1:
            return None
        new_body = dict(body)
        new_body["max_tokens"] = self.floor
        return new_body

    def resolved(self, result: dict) -> bool:
        if not result.get("ok"):
            return False
        shape_ok, content, _finish_reason = _get_message_content(result.get("data"))
        return shape_ok and bool((content or "").strip())


# ── Default registry (backwards-compatible with the pre-refactor behavior) ──

DEFAULT_REGISTRY = RepairRegistry([
    StructuredOutputRelaxation(),
    TokenStarvationBoost(),
])


# ── Dynamic loading of a user's own repairs file ─────────────────────────

def load_custom_repairs(module_path: str, registry: RepairRegistry) -> None:
    """Load *module_path* as a Python module and call its ``register()``.

    This is how a user plugs in a strategy for a new vendor's failure
    shape without touching this file or ``race_proxy_core.py`` at all,
    write a standalone ``.py`` file anywhere, define one or more
    :class:`RepairStrategy` subclasses, and a module-level::

        def register(registry: RepairRegistry) -> None:
            registry.register(MyStrategy())

    Point ``custom_repairs_module`` at the file's path in your proxy
    config (JSON or YAML) and it loads automatically at startup, added
    on top of :data:`DEFAULT_REGISTRY`. See ``examples/custom_repairs_example.py``
    in this repo for a complete, runnable template.

    Raises RuntimeError with a clear message if the file can't be loaded
    or doesn't define ``register``, fails loudly at startup rather than
    silently skipping a user's intended repair.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hermes_race_proxy_custom_repairs", module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load custom repairs module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "register"):
        raise RuntimeError(
            f"{module_path} must define a module-level "
            f"register(registry: RepairRegistry) -> None function"
        )
    module.register(registry)
    logger.info("Loaded custom repairs from %s", module_path)
