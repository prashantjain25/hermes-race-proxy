#!/usr/bin/env python3
"""
hermes-race-proxy: provider contracts
=========================================

A "provider" is one LLM vendor's connection contract: its base_url, how
it wants auth presented (or that it needs none), any headers it insists
on, and how to list/probe its own catalog of models. This is the layer
below ``discovery.py``, discovery answers "which models should race,"
providers answer "how do I even talk to this vendor at all."

Splitting it out matters because the answer to "how do I talk to this
vendor" is STABLE (opencode.ai/zen's keyless free models, OpenRouter's
Bearer-token + `:free` suffix convention, NVIDIA's trial-key catalog)
while "which specific models should race" is a policy call that
changes per user, per session, per mood. Mixing the two, like the
first version of ``examples/custom_discovery_example.py`` did, with
opencode.ai/zen's base_url and headers hardcoded inline next to the
NVIDIA-ranking logic, means every new provider you want to add
requires re-deriving its connection contract from scratch instead of
writing it once and reusing it everywhere.

Each concrete provider in this package (``providers/http/opencode.py``,
``providers/http/openrouter.py``, ``providers/http/deepinfra.py``,
``providers/http/nvidia_build.py``, ``providers/http/gcp.py``) is a small,
independently-useful object:
it can list its own catalog (where the vendor exposes one) and build a
:class:`~race_proxy_core.Backend` for any model ID, with the right
auth/headers already wired. ``providers/pool.py`` is the layer above
that turns N enabled providers × M models-per-provider into one flat
list of backends to race, the N×M matrix this module's docstring
promises.

Nothing in this package is imported by ``race_proxy_core.py`` or
``discovery.py``, it is a convenience layer for writing YOUR OWN
``custom_discovery_module`` (or standalone tooling) on top of, not a
required dependency of the proxy itself. See
``examples/provider_pool_example.py`` for the full N×M pool wired up as
a real, runnable discovery script.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import urllib.error
import urllib.request
from typing import List, Optional

logger = logging.getLogger("race_proxy.providers")


class Provider:
    """One vendor's connection contract.

    Subclass this per vendor (see the concrete providers in this
    package) rather than instantiating it directly, the base class
    defines the shape every provider shares, but ``list_models`` in
    particular needs vendor-specific parsing of that vendor's own
    ``/v1/models`` response shape (they are not all identical, even
    among nominally "OpenAI-compatible" vendors).
    """

    #: Short, stable identifier, used as a prefix in Backend names
    #: (e.g. "opencode-zen", "openrouter", "deepinfra", "nvidia-build").
    name: str = "base"

    #: OpenAI-compatible base URL, e.g. "https://openrouter.ai/api/v1".
    #: Verified live for every concrete provider in this package,
    #: see each file's own docstring for how it was checked.
    base_url: str = ""

    #: Whether this vendor's chat-completions endpoint requires an
    #: API key at all. False for opencode.ai/zen's keyless free
    #: models; True for OpenRouter, DeepInfra, and NVIDIA's trial
    #: catalog, confirmed live: all three return HTTP 401
    #: "missing/invalid API key" on a keyless chat-completions call,
    #: even for their nominally free-tier models.
    requires_api_key: bool = True

    def default_headers(self) -> dict:
        """Extra headers this vendor wants on every request, beyond
        Content-Type/Authorization (which the caller adds). Override
        for vendors with their own conventions (OpenRouter recommends
        HTTP-Referer/X-Title for attribution; opencode.ai/zen's free
        tier is sensitive to a plausible User-Agent).
        """
        return {}

    def list_models(self, api_key: str = "", timeout: float = 15.0) -> List[str]:
        """Return every model ID this vendor's catalog currently
        advertises, via its own ``/v1/models`` listing endpoint.

        Raises on any failure (network, auth, unexpected response
        shape), callers should treat that as "this provider is
        currently unavailable for discovery," not silently return an
        empty list, so the caller's own fallback logic (see
        ``discovery.py``'s documented contract) can decide what to do.
        """
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/models",
            headers=self._auth_headers(api_key),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return [m["id"] for m in data.get("data", [])]

    def _auth_headers(self, api_key: str) -> dict:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        headers.update(self.default_headers())
        return headers

    def build_backend(self, model_id: str, api_key: str = "", name: Optional[str] = None):
        """Construct a :class:`~race_proxy_core.Backend` for *model_id*
        on this provider, with this provider's auth/headers contract
        already applied.

        Imports ``Backend`` locally (not at module level) to avoid a
        circular import: ``race_proxy_core.py`` never imports this
        package, but scripts using this package typically also import
        ``race_proxy_core.Backend`` directly, and importing it at
        providers/base.py's module level would require
        ``race_proxy_core`` to already be on ``sys.path`` even for
        callers who only want the pure provider metadata (e.g. just
        calling ``list_models()``).
        """
        from race_proxy_core import Backend

        return Backend(
            name=name or f"{self.name}-{model_id.split('/')[-1]}",
            base_url=self.base_url,
            model=model_id,
            api_key=api_key,
            headers=self.default_headers(),
        )


def probe_model(
    provider: Provider, model_id: str, api_key: str = "", timeout: float = 20.0,
) -> dict:
    """Real lightweight chat-completion call to check if *model_id* on
    *provider* is alive and how fast it responds.

    Returns ``{"ok": bool, "latency": float, "error": str|None}``, the
    same shape ``discovery.probe_endpoint`` uses (this is effectively a
    provider-aware wrapper around it), for use inside
    :func:`providers.pool.build_pool`'s ranking step.
    """
    import time as _time

    url = f"{provider.base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    headers.update(provider._auth_headers(api_key))
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with the single word OK"}],
        "max_tokens": 20,
    }).encode()

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    t0 = _time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return {"ok": True, "latency": _time.time() - t0, "error": None}
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()[:300]
        except Exception:
            err_body = str(e)
        return {"ok": False, "latency": _time.time() - t0, "error": f"HTTP {e.code}: {err_body}"}
    except Exception as e:
        return {"ok": False, "latency": _time.time() - t0, "error": str(e)}


def rank_models(
    provider: Provider, model_ids: List[str], api_key: str = "",
    max_workers: int = 12, timeout: float = 20.0,
) -> List[str]:
    """Probe every model ID in *model_ids* on *provider* IN PARALLEL,
    return the ones that responded successfully, fastest first.

    Shared by every concrete provider's discovery use, this is the
    "exhaustive check first" step ``providers/pool.py`` calls once per
    enabled provider.
    """
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        futures = {
            ex.submit(probe_model, provider, model_id, api_key, timeout): model_id
            for model_id in model_ids
        }
        for fut in concurrent.futures.as_completed(futures):
            model_id = futures[fut]
            results[model_id] = fut.result()

    successful = [(m, r["latency"]) for m, r in results.items() if r["ok"]]
    successful.sort(key=lambda pair: pair[1])
    return [m for m, _latency in successful]
