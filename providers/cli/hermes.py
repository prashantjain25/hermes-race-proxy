#!/usr/bin/env python3
"""
hermes.py, Hermes Agent CLI as a race backend, via CliCaller.

This is the CLI we are ACTUALLY testing race_proxy against in this
repo's own dev environment (hermes -z "..." is the one-shot
invocation used throughout this session's live verification), yet it
had no providers/cli/ file of its own, an oversight fixed here. Same
CLI-only case claude.py and opencode.py in this directory cover:
Hermes Agent has no bare HTTP chat-completions API of its own to
point HttpCaller at, only this CLI (though Hermes DOES front other
providers' HTTP APIs internally, that's a layer beneath this CLI, not
something race_proxy can reach directly).

Verified live on this machine (Sep 2026):
- ``hermes --version`` -> Hermes Agent v0.20.6 (2026.8.27).
- ``hermes -z "<prompt>"`` (oneshot mode) is a real, working
  non-interactive call: confirmed live, plain text on stdout, exit
  code 0 on success. ``hermes -z "reply with exactly: PING_OK"``
  returned exactly ``PING_OK``, confirmed character-for-character.
- ``-m/--model`` and ``--provider`` override the model/provider for
  that one invocation only (session config in config.yaml is
  untouched). ``--reasoning LEVEL`` accepts none/minimal/low/medium/
  high/xhigh/max/ultra, confirmed accepted live (not necessarily
  every value tested against every model/provider).
- Output is plain text, not JSON, unlike ``claude -p --output-format
  json``. The response_parser below wraps stdout into the OpenAI
  chat.completion shape response_contracts.py expects, same pattern
  as opencode.py in this directory.
- On failure, Hermes prints an error message to stdout (not always
  stderr) and still exits 0 in some cases (e.g. "API call failed
  after 3 retries: HTTP 503: ..." was observed on stdout with exit
  code 0 during this session, an upstream provider being down, not a
  hermes CLI bug). The response_parser below treats any stdout
  starting with "API call failed" as an error regardless of exit
  code, since exit code alone is not a reliable success signal for
  this CLI.
"""
from __future__ import annotations

import json
from typing import Optional

from callers.cli_caller import CliCaller

#: Prefix Hermes CLI's own error output starts with, observed live
#: (a real 503 from an upstream provider surfaced this way). Exit code
#: alone is not reliable for this CLI, checked in addition to
#: returncode below.
_ERROR_PREFIX = "API call failed"


def hermes_response_parser(stdout: bytes, stderr: bytes, returncode: int) -> tuple[int, bytes]:
    """Wrap hermes -z's plain-text stdout into an OpenAI chat.completion
    shape. See this module's docstring: exit code 0 does not guarantee
    success for this CLI, the text itself must also be checked.
    """
    text = stdout.decode(errors="replace").strip()

    if returncode != 0:
        err = (stderr.decode(errors="replace") or text)[:500]
        return 500, json.dumps({"error": {"message": err, "type": "cli_error"}}).encode()

    if text.startswith(_ERROR_PREFIX):
        return 500, json.dumps({"error": {"message": text[:500], "type": "cli_error"}}).encode()

    fake_completion = {
        "id": "hermes-cli",
        "model": "hermes-cli",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
    }
    return 200, json.dumps(fake_completion).encode()


def _extract_prompt(payload_bytes: bytes) -> str:
    """Pull the last user message's text out of an incoming OpenAI-shaped
    request body, ``hermes -z`` wants a plain prompt string as an argv
    token, not a JSON messages array. Same extraction as claude.py and
    opencode.py in this directory.
    """
    try:
        body = json.loads(payload_bytes.decode(errors="replace"))
        messages = body.get("messages") or []
        return str(messages[-1].get("content", "")) if messages else ""
    except Exception:
        return payload_bytes.decode(errors="replace")


class HermesCliCaller(CliCaller):
    """CliCaller preconfigured for ``hermes -z "<prompt>"``'s argv shape
    (prompt as an argv token, not stdin), mirrors ClaudeCliCaller and
    OpenCodeCliCaller in this directory for the same reason: most CLIs
    read a prompt from stdin (CliCaller's default assumption), this
    one doesn't.

    *model* and *provider* are optional, passed through as ``-m`` and
    ``--provider`` when set, per Hermes's own ``--help`` output.
    *reasoning* is optional, passed as ``--reasoning`` (e.g.
    ``"minimal"``), the same reasoning-effort concern already wired
    into providers/http/gcp.py's extra_body for Gemini's HTTP path,
    here it's a native CLI flag instead.
    """

    def __init__(
        self,
        hermes_bin: str = "hermes",
        model: Optional[str] = None,
        provider: Optional[str] = None,
        reasoning: Optional[str] = None,
        cwd: Optional[str] = None,
    ):
        super().__init__(command_template=[hermes_bin], response_parser=hermes_response_parser, cwd=cwd)
        self.hermes_bin = hermes_bin
        self.model = model
        self.provider = provider
        self.reasoning = reasoning

    def call(self, payload_bytes: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
        import subprocess

        prompt = _extract_prompt(payload_bytes)
        argv = [self.hermes_bin, "-z", prompt]
        if self.model:
            argv += ["-m", self.model]
        if self.provider:
            argv += ["--provider", self.provider]
        if self.reasoning:
            argv += ["--reasoning", self.reasoning]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=self.cwd)
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"hermes CLI timed out after {timeout}s") from e
        except FileNotFoundError as e:
            raise RuntimeError(
                f"hermes CLI not found on PATH ({self.hermes_bin!r}), is it installed?"
            ) from e
        return self.response_parser(proc.stdout, proc.stderr, proc.returncode)


def build_hermes_backend(
    name: str = "hermes-cli",
    hermes_bin: str = "hermes",
    model: Optional[str] = None,
    provider: Optional[str] = None,
    reasoning: Optional[str] = None,
):
    """Construct a Backend that races Hermes Agent's own CLI via
    ``hermes -z``, confirmed live and working (unlike claude.py's and
    opencode.py's helpers in this directory, which hit account-level
    auth blocks during their own live checks). See this module's
    docstring for the exit-code caveat before wiring this into a
    production pool without also checking response content.
    """
    from race_proxy_core import Backend

    return Backend(
        name=name,
        base_url="cli://hermes-agent",  # cosmetic only, HermesCliCaller ignores it
        model=model or "hermes-cli",
        caller=HermesCliCaller(hermes_bin=hermes_bin, model=model, provider=provider, reasoning=reasoning),
    )
