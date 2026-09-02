#!/usr/bin/env python3
"""
opencode.py, OpenCode CLI as a race backend, via CliCaller.

NOT the same thing as providers/http/opencode.py (opencode.ai/zen, a
hosted model-serving endpoint reached over plain HTTP). This file is
for OpenCode the CODING AGENT CLI (binary: ``opencode``,
https://opencode.ai), an entirely different product from the same
name, wired in here because it has no HTTP chat-completions API of
its own, only its CLI, the same CLI-only case claude.py in this
directory covers for Claude Code.

Verified live on this machine (Sep 2026):
- ``opencode --version`` -> 1.18.20, installed at
  /opt/homebrew/bin/opencode.
- ``opencode run "<prompt>"`` is a real one-shot, non-interactive
  command (confirmed it launches, selects a model, and exits without
  needing a TUI or pty). The specific call made during this check
  failed with "insufficient credits" on the configured provider
  account, an account-billing issue, not a wiring problem, so actual
  successful model output has NOT been captured end to end here.
- ``opencode run`` prints plain text to stdout on success (not JSON by
  default; ``--format json`` exists per the opencode skill's flag
  table but was not exercised live here), the response_parser below
  wraps whatever text comes back into the OpenAI chat.completion shape
  response_contracts.py expects, same pattern as
  examples/cli_caller_example.py's echo-based demo.

Re-verify against an account with available credits before relying on
this in production.
"""
from __future__ import annotations

import json
from typing import Optional

from callers.cli_caller import CliCaller


def opencode_response_parser(stdout: bytes, stderr: bytes, returncode: int) -> tuple[int, bytes]:
    """Wrap opencode run's plain-text stdout into an OpenAI
    chat.completion shape. See this module's docstring: not yet
    verified against a successful (non-error) run.
    """
    if returncode != 0:
        err = stderr.decode(errors="replace")[:500]
        return 500, json.dumps({"error": {"message": err, "type": "cli_error"}}).encode()

    text = stdout.decode(errors="replace").strip()
    fake_completion = {
        "id": "opencode-cli",
        "model": "opencode-cli",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
    }
    return 200, json.dumps(fake_completion).encode()


def _extract_prompt(payload_bytes: bytes) -> str:
    """Same extraction as claude.py: pull the last user message's text
    out of the incoming OpenAI-shaped request body, ``opencode run``
    wants a plain prompt string as an argv token, not a JSON body.
    """
    try:
        body = json.loads(payload_bytes.decode(errors="replace"))
        messages = body.get("messages") or []
        return str(messages[-1].get("content", "")) if messages else ""
    except Exception:
        return payload_bytes.decode(errors="replace")


class OpenCodeCliCaller(CliCaller):
    """CliCaller preconfigured for ``opencode run "<prompt>"``'s argv
    shape (prompt as an argv token, not stdin), mirrors
    ClaudeCliCaller in claude.py for the same reason: most CLIs read a
    prompt from stdin (CliCaller's default assumption), this one
    doesn't.

    *model* is optional and passed through as ``--model
    provider/model`` when set, per the opencode skill's flag table;
    left unset here by default so opencode uses whatever its own
    config/auth already selects.
    """

    def __init__(self, opencode_bin: str = "opencode", model: Optional[str] = None, cwd: Optional[str] = None):
        super().__init__(command_template=[opencode_bin], response_parser=opencode_response_parser, cwd=cwd)
        self.opencode_bin = opencode_bin
        self.model = model

    def call(self, payload_bytes: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
        import subprocess

        prompt = _extract_prompt(payload_bytes)
        argv = [self.opencode_bin, "run", prompt]
        if self.model:
            argv += ["--model", self.model]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=self.cwd)
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"opencode CLI timed out after {timeout}s") from e
        except FileNotFoundError as e:
            raise RuntimeError(
                f"opencode CLI not found on PATH ({self.opencode_bin!r}), is it installed?"
            ) from e
        return self.response_parser(proc.stdout, proc.stderr, proc.returncode)


def build_opencode_backend(name: str = "opencode-cli", opencode_bin: str = "opencode", model: Optional[str] = None):
    """Construct a Backend that races OpenCode's coding-agent CLI. See
    this module's docstring for the verified-vs-unverified boundary
    (account credits blocked a full live check here) before trusting
    this in production.
    """
    from race_proxy_core import Backend

    return Backend(
        name=name,
        base_url="cli://opencode",  # cosmetic only, OpenCodeCliCaller ignores it
        model=model or "opencode-cli",
        caller=OpenCodeCliCaller(opencode_bin=opencode_bin, model=model),
    )
