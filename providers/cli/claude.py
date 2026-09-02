#!/usr/bin/env python3
"""
claude.py, Claude Code CLI as a race backend, via CliCaller.

The CLI-only case callers/cli_caller.py's docstring describes: Claude
Code (binary: ``claude``) has no public HTTP chat-completions API of
its own to point HttpCaller at, only this CLI. ``claude -p`` (print
mode) runs one prompt and exits, exactly the request/response shape a
single race attempt needs, no multi-turn session management required.

Verified live on this machine (Sep 2026):
- ``claude --version`` -> 2.1.252 (Claude Code), installed at
  /Users/dolphin/.local/bin/claude.
- ``claude -p "..." --output-format json --max-turns 1`` returns real,
  well-formed JSON on this exact machine: a top-level object with
  ``result`` (the text), ``session_id``, ``subtype``, ``is_error``, and
  usage/cost fields. Confirmed the SHAPE live; the specific call made
  during this check failed AUTH ("OAuth session expired"), not a
  parsing issue, so the actual prompt text in ``result`` has not been
  content-verified end to end here, only the JSON envelope has.
- Real chat completion payloads (the OpenAI-shaped ``messages: [...]``
  body every other Backend in this repo sends) are NOT what Claude
  Code's CLI wants on stdin, it wants a plain prompt string. The
  response_parser below extracts ``messages[-1].content`` from the
  incoming OpenAI-shaped body and passes THAT as the prompt, translating
  between the two conventions at this one boundary, same as any CLI
  integration must (see cli_caller.py's docstring on why a CLI's
  stdin/stdout shape is a per-vendor decision, not a generic one).

Re-verify against a currently-authenticated ``claude`` session before
relying on this in production, this file has NOT been proven to
successfully return real model content end to end, only that the
process launches, accepts the flags below, and produces the documented
JSON shape.
"""
from __future__ import annotations

import json
from typing import Optional

from callers.cli_caller import CliCaller


def claude_response_parser(stdout: bytes, stderr: bytes, returncode: int) -> tuple[int, bytes]:
    """Parse ``claude -p ... --output-format json``'s stdout into the
    OpenAI chat.completion shape ``response_contracts.py`` already
    knows how to read (same synthetic-status convention every caller
    uses, see callers/cli_caller.py's default_response_parser).
    """
    if returncode != 0:
        err = stderr.decode(errors="replace")[:500]
        return 500, json.dumps({"error": {"message": err, "type": "cli_error"}}).encode()

    try:
        envelope = json.loads(stdout.decode(errors="replace"))
    except Exception as e:
        return 500, json.dumps({
            "error": {"message": f"claude CLI output was not valid JSON: {e}", "type": "cli_parse_error"}
        }).encode()

    if envelope.get("is_error"):
        return 500, json.dumps({
            "error": {"message": envelope.get("result", "unknown claude CLI error"), "type": "cli_error"}
        }).encode()

    fake_completion = {
        "id": envelope.get("session_id", "claude-cli"),
        "model": "claude-code-cli",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": envelope.get("result", "")},
            "finish_reason": "stop",
        }],
    }
    return 200, json.dumps(fake_completion).encode()


def _extract_prompt(payload_bytes: bytes) -> str:
    """Pull the last user message's text out of an incoming OpenAI-shaped
    request body, since ``claude -p`` wants a plain prompt string on
    argv, not a JSON messages array.
    """
    try:
        body = json.loads(payload_bytes.decode(errors="replace"))
        messages = body.get("messages") or []
        return str(messages[-1].get("content", "")) if messages else ""
    except Exception:
        return payload_bytes.decode(errors="replace")


class ClaudeCliCaller(CliCaller):
    """CliCaller preconfigured for the ``claude`` CLI's actual argv
    shape: the prompt is an ARGV TOKEN (``claude -p "<prompt>"``), not
    piped to stdin the way callers/cli_caller.py's default assumes for
    most CLIs, so this subclass overrides ``call`` to build argv per
    request instead of using a fixed ``command_template``.

    *claude_bin* defaults to ``"claude"`` (resolved via PATH); pass an
    absolute path if you need to pin a specific binary (see
    callers/base.py's Caller docstring and the claude-code skill's
    "Binary Resolution" guidance for why that can matter).
    """

    def __init__(self, claude_bin: str = "claude", max_turns: int = 1, cwd: Optional[str] = None):
        # command_template is unused by our overridden call() below,
        # kept only so CliCaller.__init__'s bookkeeping stays intact.
        super().__init__(command_template=[claude_bin], response_parser=claude_response_parser, cwd=cwd)
        self.claude_bin = claude_bin
        self.max_turns = max_turns

    def call(self, payload_bytes: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
        import subprocess

        prompt = _extract_prompt(payload_bytes)
        argv = [
            self.claude_bin, "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(self.max_turns),
        ]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=self.cwd)
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"claude CLI timed out after {timeout}s") from e
        except FileNotFoundError as e:
            raise RuntimeError(
                f"claude CLI not found on PATH ({self.claude_bin!r}), is it installed?"
            ) from e
        return self.response_parser(proc.stdout, proc.stderr, proc.returncode)


def build_claude_backend(name: str = "claude-cli", claude_bin: str = "claude"):
    """Construct a Backend that races Claude Code's CLI, see this
    module's docstring for the verified-vs-unverified boundary before
    trusting this in production.
    """
    from race_proxy_core import Backend

    return Backend(
        name=name,
        base_url="cli://claude-code",  # cosmetic only, ClaudeCliCaller ignores it
        model="claude-code-cli",
        caller=ClaudeCliCaller(claude_bin=claude_bin),
    )
