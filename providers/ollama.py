#!/usr/bin/env python3
"""
Ollama provider, local, self-hosted models. No cloud vendor at all.

The providers/ layer isn't limited to cloud API vendors with rate
limits and trial terms. Anything speaking the OpenAI Chat Completions
shape qualifies, including a model running on your own machine or LAN
with zero network egress. Ollama is the reference example here; the
same three-line pattern (base_url, a dummy api_key if the runtime
insists on seeing one, requires_api_key) covers LM Studio, vLLM's
OpenAI-compatible server, llama.cpp's server mode, and text-generation-
webui's OpenAI extension, all of which implement the same wire format.

Verified against Ollama's own documentation (docs.ollama.com/api/openai-
compatibility, Feb 2024 announcement + current docs):
- base_url http://localhost:11434/v1 (default port; change host/port to
  match your own instance).
- /v1/chat/completions and /v1/models are both supported.
- api_key is "required but ignored" by Ollama's client examples, no
  real authentication happens locally, but the OpenAI SDK shape wants
  a non-empty string in the field, so this provider supplies a dummy
  one rather than leaving it unset.
- requires_api_key is left False here since there's no real credential
  to gate on; the dummy key is sent unconditionally regardless of what
  the caller passes.

Racing a local model alongside cloud backends is a genuinely different
value proposition from racing two cloud vendors against each other: a
local model has zero per-token cost, zero data leaving your machine,
and no rate limit from a shared trial pool, at the cost of needing your
own hardware and being (typically) slower or lower-quality than a
hosted flagship. Pooling it as ONE MORE racer, not a replacement, lets
the fastest-available answer win regardless of which side of that
tradeoff it came from.
"""
from __future__ import annotations

from providers.base import Provider


class OllamaProvider(Provider):
    name = "ollama"
    base_url = "http://localhost:11434/v1"
    requires_api_key = False

    def _auth_headers(self, api_key: str) -> dict:
        # Ollama's own client examples send a placeholder key
        # ("ollama") because the OpenAI SDK shape expects a non-empty
        # Authorization value, even though Ollama does not actually
        # check it. Send that placeholder unconditionally so this
        # provider works whether or not a real key was ever configured
        # (there is none to configure).
        headers = {"Authorization": "Bearer ollama"}
        headers.update(self.default_headers())
        return headers
