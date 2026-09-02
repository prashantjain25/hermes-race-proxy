#!/usr/bin/env python3
"""
hermes-race-proxy: provider response contracts (Adapter, reactive not speculative)
=====================================================================================

Why this file exists
---------------------
``race_proxy_core.py`` used to treat "HTTP 200 + json.loads() succeeded"
as success. That is not the same thing as "this backend actually
answered the question." A 200 can legitimately wrap a failure:

  - SSE bytes that happen to still round-trip through json.loads() on
    some vendors' error envelopes (seen live: opencode.ai returning a
    200 whose body is Server-Sent-Events text, not one JSON object,
    when ``stream`` leaked through — see race_proxy_core.py's
    ``body["stream"] = False`` fix; this module is the second,
    independent layer of defense for cases that fix doesn't cover).
  - A reasoning model that spent its entire token budget on a hidden
    thinking trace and returned ``content: ""`` with
    ``finish_reason: "length"``.
  - A response blocked by a safety/content filter
    (``finish_reason: "content_filter"/"safety"/"recitation"``) — a
    different failure than starvation, needing a different response
    (no retry with a bigger budget, that will never help).
  - A vendor that answers 200 with an empty ``choices`` array, or a
    ``choices[0].message`` shape that isn't quite OpenAI's, because
    "OpenAI-compatible" is a spectrum, not a guarantee.

None of that is detectable from the HTTP status line alone. It's a
translation problem, not an "add an operation to a fixed set of
classes" problem — which is why this is an Adapter, not a Visitor:
Visitor fits a STABLE set of element classes with a GROWING set of
operations (double dispatch, ``element.accept(visitor)``); we have the
opposite — exactly ONE operation ("normalize raw bytes into
ParsedResponse") against a GROWING, OPEN set of external wire formats.
That is the textbook Adapter shape: translate an external interface
into the interface your code already depends on.

Adapter, reactive not speculative
------------------------------------
The pattern itself does not change as backends are added — it stays
Adapter. What changes is WHEN a new adapter subclass gets written.
:class:`GenericOpenAIContract` below is one adapter that already
absorbs every failure-shape-inside-a-200 pattern actually observed
(reasoning starvation under several field-name conventions, safety
blocks, streaming-delta leakage) as generic, vendor-agnostic checks —
because those patterns are genuinely shared across the OpenAI-
compatible ecosystem, not because we're avoiding Adapter. A NEW
``ProviderContract`` subclass gets written reactively, the same way
Hermes's own ``extract_content_or_reasoning()`` grew its
``reasoning_details`` (OpenRouter array) branch only once real
OpenRouter traffic proved the two-field check wasn't enough, and the
same way opencode/crush-style CLIs patch their own client when an
upstream provider's actual behavior changes, not ahead of time for
providers that might exist someday. Pre-writing
``providers_contracts/devpass.py`` / ``gcp.py`` / ``openai.py`` before
any of them has shown a response shape the generic contract can't
handle would be the speculative version of this pattern — see
:class:`ProviderContract`'s docstring, "Escape hatch" section, for the
actual bar a real observed failure has to clear first.

Contract
--------
A :class:`ProviderContract` subclass takes raw response bytes (a 200
body ONLY — HTTP-level errors never reach here, ``Backend._do_request``
already returns those as ``ok=False`` before contracts get involved)
and returns a :class:`ParsedResponse`:

  - ``ok=True``  -> ``content`` is real, ``data`` is a canonical
    OpenAI-shaped dict (so every downstream consumer —
    ``_response_is_usable``, ``repairs.py``, the HTTP handler — keeps
    working unmodified regardless of which vendor answered).
  - ``ok=False`` -> ``error`` explains WHY (e.g. "reasoning consumed
    entire budget, content empty"), not a generic ``str(exception)``.

Versioning a contract when its wire format changes
-------------------------------------------------------
When a REAL, OBSERVED failure proves an existing contract (generic or
vendor-specific) needs to change:

  1. Do NOT edit the existing ``<X>ContractV<N>`` class in place — a
     live proxy process may still be mid-request against the OLD
     shape, and a clean diff between versions matters for the next
     person debugging a regression.
  2. Add a NEW class ``<X>ContractV<N+1>`` in the same file, with a
     docstring explaining what changed, when it was observed, and
     (for anything reseller-routed) how it was verified — see any
     vendor-specific contract file's "MANDATORY BEFORE ADDING OR
     CHANGING A VERSION" section for what "verified" means when a
     bulk reseller sits between you and the actual vendor.
  3. Point that file's module-level ``CURRENT`` binding at the new
     class — the only symbol a registry ever imports, so bumping it is
     the entire upgrade.
  4. Leave the old class in place, unreferenced. The file reads
     top-to-bottom as a dated changelog, not archaeology through git
     blame.

Registering a contract
-----------------------
Call ``registry.register("<backend-name-from-config>", contract)`` —
matching key is a backend's ``name`` field in ``race_proxy.local.json``
(e.g. ``"nemotron"``, ``"gemini"``), not the model string. Nothing is
pre-registered by default (see :func:`_build_default_registry`) —
every backend gets :class:`GenericOpenAIContract` until a real failure
justifies registering something else.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger("race_proxy.response_contracts")


# ── Reuse Hermes's own reasoning-extraction logic, don't re-derive it ────
#
# hermes-agent already maintains the canonical list of "where a reasoning
# model hides its thinking trace when content is empty" in
# agent.auxiliary_client.extract_content_or_reasoning() — it knows about
# THREE shapes (message.reasoning, message.reasoning_content, AND
# OpenRouter's message.reasoning_details array format), plus inline
# <think>/<thinking>/<reasoning>/<thought> tag stripping for models that
# echo reasoning inside `content` itself instead of a separate field.
# That is strictly more than this file independently knew about — the
# whole point of "check Hermes source for where this is handled" is to
# not let this proxy's copy silently drift out of sync with the one
# actually running in production.
#
# race-proxy is a standalone repo (no installable hermes-agent package,
# confirmed: not on PyPI, `agent.auxiliary_client` only importable from
# INSIDE a hermes-agent checkout), and that function takes an SDK
# response object (attribute access: response.choices[0].message.content)
# not a raw parsed dict, so it can't be called as-is from here. This
# wrapper does the two honest things available:
#   1. If this proxy happens to be running with hermes-agent importable
#      on sys.path (the common case: race-proxy started from inside a
#      hermes-agent checkout, which is how it's actually deployed here),
#      call the REAL function via a tiny attribute-access shim so it's
#      always the current logic, not a copy.
#   2. Otherwise, fall back to a mirror of that function's field list
#      ONLY (not a reimplementation of its full logic), clearly marked
#      with the source line it was copied from and the date, so the next
#      person updating this file knows to re-sync it if Hermes's list
#      ever grows again.
_HERMES_REASONING_ALIASES_SOURCE = (
    "agent.auxiliary_client.extract_content_or_reasoning, hermes-agent "
    "as of 2026-09-02: checks message.reasoning, message.reasoning_content "
    "(direct fields), then message.reasoning_details (OpenRouter array "
    "format, each entry's .summary/.content/.text). If this list has grown "
    "since, update both this comment and MIRROR_REASONING_FIELD_ALIASES below."
)
MIRROR_REASONING_FIELD_ALIASES = ("reasoning", "reasoning_content")


def extract_reasoning_text(msg: dict) -> Optional[str]:
    """Best-effort reasoning-trace extraction from a parsed message dict.

    Tries the REAL Hermes function first (see module comment above for
    why that's only possible when hermes-agent is importable), degrades
    to the synced field-alias mirror otherwise. Does not attempt the
    ``reasoning_details`` (OpenRouter array) shape in the fallback path —
    that's the concrete reason to prefer the real function when available
    rather than trusting the mirror is complete forever.
    """
    try:
        from agent.auxiliary_client import extract_content_or_reasoning
        # extract_content_or_reasoning expects response.choices[0].message
        # attribute access, not a dict — build the minimal shim it needs.
        fake_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=msg.get("content"),
                reasoning=msg.get("reasoning"),
                reasoning_content=msg.get("reasoning_content"),
                reasoning_details=msg.get("reasoning_details"),
            ))]
        )
        # This returns content-or-reasoning combined; we only want it
        # when our own content check already found content empty, so
        # this is only ever called from that branch (see call sites).
        result = extract_content_or_reasoning(fake_response)
        return result or None
    except ImportError:
        pass
    for field_name in MIRROR_REASONING_FIELD_ALIASES:
        val = msg.get(field_name)
        if val:
            return val
    return None


# ── The extension contract (Adapter target) ──────────────────────────────

@dataclass
class ParsedResponse:
    """Canonical, vendor-agnostic verdict on a 200 response body.

    ``data`` is always OpenAI chat.completion shaped when ``ok=True``,
    regardless of what the vendor actually sent — that's the whole
    point of the adapter: race_proxy_core.py, repairs.py, and the HTTP
    handler read ``data["choices"][0]["message"]["content"]`` and never
    need to know which vendor answered.
    """
    ok: bool
    content: Optional[str] = None
    finish_reason: Optional[str] = None
    reasoning_content: Optional[str] = None
    data: Optional[dict] = None
    error: Optional[str] = None


class ProviderContract:
    """A response-shape translator. Base class for the generic default
    AND for the rare true vendor-specific exception.

    Do not instantiate this base class directly — it has no parsing
    logic, ``parse()`` raises. See :class:`GenericOpenAIContract` below
    for the contract almost every backend actually uses.

    Escape hatch: when a vendor genuinely needs its own file
    ------------------------------------------------------------
    Do NOT write ``providers_contracts/<vendor>.py`` just because
    you're adding a new backend. :class:`GenericOpenAIContract`
    already covers every failure-shape-inside-a-200 pattern actually
    observed across vendors (reasoning-budget starvation under any of
    several field names, safety/content-filter blocks, streaming-delta
    leakage). Writing a new per-vendor file is the exception, justified
    ONLY when a vendor's response shape cannot be expressed as a
    generic pattern — e.g. it nests content one level deeper than
    ``choices[0].message.content``, uses a completely different
    envelope (not OpenAI-Chat-Completions-shaped at all), or requires
    a stateful multi-step handshake the generic contract's one-shot
    ``parse()`` can't express. "This vendor has one quirky field name"
    is NOT sufficient justification — add the field name to
    :attr:`GenericOpenAIContract.REASONING_FIELD_ALIASES` (or the
    equivalent generic list for whatever the new pattern is) instead.
    This is the actual answer to "we'll keep adding vendors forever":
    the generic contract is designed to absorb new *patterns*, not
    grow one file per *vendor*.
    """

    #: Matches a backend's ``name`` field in config (see module
    #: docstring "Quick start" above for why name, not model string).
    name: str = "base"

    #: Human-readable, bumped every time a new ContractV<N> class is
    #: added for this vendor (see module docstring's versioning
    #: convention). Shows up in error strings so a failure log tells
    #: you which contract version was active, not just which vendor.
    version: str = "v0"

    def looks_like_sse(self, raw: bytes) -> bool:
        """True if *raw* starts like an SSE stream, not a JSON object.

        Shared across every contract (not vendor-specific behavior —
        SSE-shaped bytes mean the same thing regardless of vendor: the
        upstream streamed despite ``stream: false``, or a proxy in
        between re-wrapped the response). Kept here so no per-vendor
        file has to reimplement this check.
        """
        head = raw[:32].lstrip()
        return head.startswith(b"data:") or head.startswith(b"event:") or head.startswith(b": ")

    def parse(self, raw: bytes, status: int, requested_model: Optional[str] = None) -> ParsedResponse:
        """Translate *raw* 200-status bytes into a :class:`ParsedResponse`.

        *status* is passed through even though contracts only ever see
        200s today (see module docstring) — kept in the signature so a
        future vendor whose "real" success code isn't 200 doesn't need
        a signature change, only a new contract that checks it.

        *requested_model* is the model string THIS request asked for
        (e.g. ``"nemotron-3.5-lightning-free"``), passed through so a
        contract can compare it against whatever the response's own
        ``data["model"]`` echoes back — the only honest signal available
        for detecting reseller-side routing substitution (see
        ``providers_contracts/nemotron.py``'s module docstring,
        "DRIFT DETECTION" section, for why this is observational only
        and not a confirmed-vendor-contract check).
        """
        raise NotImplementedError

    def _log_model_drift(self, requested_model: Optional[str], data: Optional[dict]) -> None:
        """Shared helper: log (not raise, not reject) when the response
        body's own echoed ``model`` field disagrees with what was
        requested. This is a bulk-reseller routing signal, not a vendor
        contract violation — see the per-vendor module docstrings for
        why we log this instead of asserting anything about it.
        """
        if not requested_model or not isinstance(data, dict):
            return
        echoed = data.get("model")
        if echoed and echoed != requested_model:
            logger.warning(
                "model-drift contract=%s requested=%r echoed=%r "
                "(reseller may have routed this slug to a different backend)",
                self.name, requested_model, echoed,
            )


# ── Fallback: today's pre-contract behavior, preserved exactly ──────────

class GenericOpenAIContract(ProviderContract):
    """The ONE contract almost every backend should use. Handles every
    failure-shape-inside-a-200 pattern seen so far — reasoning-budget
    starvation, safety/content-filter blocks, streaming-delta leakage —
    generically, because none of those patterns are actually specific
    to one vendor: they are shared conventions across the OpenAI-
    compatible ecosystem (reasoning models all expose a hidden-thinking
    field under some name; ``finish_reason: content_filter/safety`` is
    a standard enum value; a leaked SSE delta chunk looks the same
    regardless of who sent it).

    Do NOT default to writing a new ``providers_contracts/<vendor>.py``
    file when you add a new backend (devpass, GCP, OpenAI direct,
    whatever's next) — see this module's docstring, "Escape hatch"
    section, for the actual bar a vendor has to clear before it
    deserves its own file. Most new backends should register nothing
    and just get this contract via the registry's fallback.

    This started as ``Backend._do_request``'s original plain
    ``json.loads`` + blank-content check, then absorbed what were
    briefly two separate per-vendor files (nemotron.py, gemini.py)
    once it became clear their logic wasn't vendor-specific at all —
    registering a genuinely vendor-unique contract remains opt-in and
    additive; an unregistered backend gets this, unchanged.
    """

    name = "generic"
    version = "v2"

    #: Standard finish_reason values meaning "the vendor's safety layer
    #: blocked this, not a token-budget problem." Shared across the
    #: OpenAI Chat Completions spec (``content_filter``) and Gemini's
    #: OpenAI-compat shim (``safety``, ``recitation``) — not one
    #: vendor's private enum. A bigger max_tokens will never fix this,
    #: so it must be distinguished from starvation before any retry
    #: logic (``repairs.TokenStarvationBoost``) wastes an attempt on it.
    SAFETY_BLOCK_FINISH_REASONS = frozenset({"content_filter", "safety", "recitation"})

    def parse(self, raw: bytes, status: int, requested_model: Optional[str] = None) -> ParsedResponse:
        if self.looks_like_sse(raw) or raw[:200].find(b'"delta"') != -1:
            head = raw[:400].decode(errors="replace")
            return ParsedResponse(
                ok=False,
                error=f"generic[{self.version}]: response is SSE/streaming-delta-shaped, "
                      f"not a complete chat.completion (stream leaked through despite "
                      f"stream:false; head={head!r})",
            )
        try:
            data = json.loads(raw)
        except Exception as e:
            head = raw[:400].decode(errors="replace")
            tail = raw[-200:].decode(errors="replace") if len(raw) > 600 else ""
            return ParsedResponse(
                ok=False,
                error=f"generic[{self.version}]: json-decode-failed error={e} head={head!r} tail={tail!r}",
            )
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            return ParsedResponse(
                ok=False, data=data,
                error=f"generic[{self.version}]: empty/missing choices array "
                      f"(likely a prompt-level block before generation started)",
            )
        try:
            choice = choices[0]
            msg = choice.get("message", {}) or {}
            content = msg.get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            return ParsedResponse(
                ok=False, data=data,
                error=f"generic[{self.version}]: unexpected choices[0] shape, "
                      f"keys={list(choices[0].keys()) if choices and isinstance(choices[0], dict) else type(choices)}",
            )

        if finish_reason in self.SAFETY_BLOCK_FINISH_REASONS:
            return ParsedResponse(
                ok=False, data=data, finish_reason=finish_reason,
                error=f"generic[{self.version}]: response blocked by safety/content filter "
                      f"(finish_reason={finish_reason!r}) — a bigger max_tokens will not fix "
                      f"this, do not route through TokenStarvationBoost",
            )

        if not content.strip():
            # reasoning-field detection delegates to extract_reasoning_text
            # (module-level function above), which reuses hermes-agent's
            # OWN extract_content_or_reasoning() when importable, instead
            # of this file maintaining its own separately-drifting alias
            # list — see that function's docstring for why.
            reasoning = extract_reasoning_text(msg)
            if reasoning:
                return ParsedResponse(
                    ok=False, data=data, finish_reason=finish_reason, reasoning_content=reasoning,
                    error=f"generic[{self.version}]: reasoning consumed entire token budget "
                          f"(a reasoning field is present, content empty, "
                          f"finish_reason={finish_reason!r}) — candidate for "
                          f"repairs.TokenStarvationBoost",
                )
            return ParsedResponse(
                ok=False, data=data, finish_reason=finish_reason,
                error=f"generic[{self.version}]: blank content, finish_reason={finish_reason!r}",
            )

        self._log_model_drift(requested_model, data)
        return ParsedResponse(ok=True, content=content, finish_reason=finish_reason, data=data)


# ── Registry: backend-name -> contract ────────────────────────────────────

class ContractRegistry:
    """Maps a backend's config ``name`` to the :class:`ProviderContract`
    that knows how to read its responses. Unregistered names fall back
    to :class:`GenericOpenAIContract` — see its docstring for why that
    is safe by construction.
    """

    def __init__(self):
        self._by_name: dict[str, ProviderContract] = {}
        self._fallback = GenericOpenAIContract()

    def register(self, backend_name: str, contract: ProviderContract) -> "ContractRegistry":
        self._by_name[backend_name] = contract
        return self

    def get(self, backend_name: str) -> ProviderContract:
        return self._by_name.get(backend_name, self._fallback)


def _build_default_registry() -> ContractRegistry:
    """Returns a registry with NOTHING vendor-specific pre-registered.

    Every backend gets :class:`GenericOpenAIContract` (this module's
    fallback) unless you explicitly call ``registry.register(name,
    contract)`` yourself. This is the deliberate answer to "we'll keep
    adding vendors forever" (devpass, GCP, OpenAI direct, ...): the
    default path does NOT grow with every new backend, because the
    generic contract already handles every failure-shape-inside-a-200
    pattern actually observed (reasoning starvation, safety blocks,
    streaming-delta leakage) without vendor-specific code. Adding a
    backend to ``race_proxy.local.json`` needs zero changes here.

    See :class:`ProviderContract`'s docstring, "Escape hatch: when a
    vendor genuinely needs its own file" section, for the (expected to
    be rare) case that justifies writing one.
    """
    return ContractRegistry()


DEFAULT_CONTRACT_REGISTRY = _build_default_registry()
