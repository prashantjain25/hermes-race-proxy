#!/usr/bin/env python3
"""
CliCaller — reach a backend through its own official CLI, not HTTP.
======================================================================

The concrete case this exists for: a vendor with no public HTTP chat-
completions API at all, only an official CLI (the devpass case, when
that backend gets added). Some vendors sell you a CLI specifically
because they do not expose a stable, documented HTTP contract for
third parties, in which case there is nothing to pool a connection to
and no OpenAI-compatible endpoint to point ``HttpCaller`` at. The CLI
IS the interface.

This caller runs the CLI as a subprocess, feeds it the request body,
and reads its stdout back as the response payload. That last part is
the part every real integration needs to configure: different CLIs
print different things (some print a raw chat-completion JSON object
compatible with response_contracts.py's GenericOpenAIContract out of
the box, some print their own wrapper format, some print plain text
with no structure at all). See *response_parser* below.

Because subprocess launches are naturally slower and noisier than a
pooled HTTP connection, this caller does NOT share the HTTP connection
pool or its stale-connection retry logic (connection_pool.py) at all,
there is no TCP connection here to be stale. It does share the same
``(status, raw_bytes)`` return contract as HttpCaller, so nothing
above this layer (Backend, race(), response_contracts.py) needs to
know or care that a CLI answered instead of an HTTP endpoint.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Callable, Optional

from callers.base import Caller


def default_response_parser(stdout: bytes, stderr: bytes, returncode: int) -> tuple[int, bytes]:
    """Default *response_parser*: assumes the CLI prints one JSON object
    on stdout, already OpenAI chat.completion shaped, and a non-zero
    exit code means failure.

    This is a REASONABLE DEFAULT, not a guarantee any given CLI matches
    it. A real integration for a specific vendor's CLI will very likely
    need its own parser function passed to :class:`CliCaller`'s
    constructor, mirroring exactly why response_contracts.py exists for
    HTTP backends whose JSON shape varies: a CLI's stdout format varies
    by vendor just as much as an HTTP body does, if not more (no
    "OpenAI-compatible" convention exists for CLI stdout at all).
    """
    if returncode != 0:
        err = stderr.decode(errors="replace")[:500]
        # Status 500 is a synthetic HTTP-shaped code for "the CLI
        # itself reported failure" — see Caller's docstring for why
        # every caller invents an HTTP-style status regardless of
        # transport, this keeps response_contracts.py's status==200
        # gate meaningful without a transport-specific branch there.
        return 500, json.dumps({"error": {"message": err, "type": "cli_error"}}).encode()
    return 200, stdout


class CliCaller(Caller):
    """Reaches a backend by running its official CLI as a subprocess.

    *command_template* is a list of argv tokens; the literal string
    ``"{stdin}"`` inside it is a marker documenting that the request
    body is piped to stdin (most CLIs read a prompt/request from stdin
    rather than accepting a full JSON body as an argv token, argv has
    length/escaping limits raw JSON payloads can blow past). If the
    real CLI you're wiring up wants the payload as a file path or an
    argv token instead of stdin, write your own :class:`Caller`
    subclass for it rather than overloading this one, that is exactly
    the kind of "this vendor needs a real exception" case
    ``ProviderContract``'s escape-hatch guidance already applies to
    (see response_contracts.py) — the same restraint applies here:
    most new CLI-based vendors should fit this class's stdin contract
    before you write a new one.

    *response_parser* defaults to :func:`default_response_parser`
    (assumes JSON-on-stdout); pass your own for a CLI with a different
    output format.
    """

    def __init__(
        self, command_template: list[str],
        response_parser: Callable[[bytes, bytes, int], tuple[int, bytes]] = default_response_parser,
        cwd: Optional[str] = None,
    ):
        self.command_template = command_template
        self.response_parser = response_parser
        self.cwd = cwd

    def call(self, payload_bytes: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
        """*headers* is accepted for interface parity with
        :class:`~callers.http_caller.HttpCaller` but unused here, a CLI
        subprocess has no HTTP headers, whatever equivalent
        configuration it needs (an API key, a model flag) belongs in
        *command_template* or the subprocess's own environment, set up
        by whoever constructs this caller for a specific vendor.
        """
        try:
            proc = subprocess.run(
                self.command_template,
                input=payload_bytes,
                capture_output=True,
                timeout=timeout,
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(
                f"CLI caller timed out after {timeout}s running {self.command_template!r}"
            ) from e
        except FileNotFoundError as e:
            raise RuntimeError(
                f"CLI caller could not find executable {self.command_template[0]!r} "
                f"on PATH, is the official CLI installed?"
            ) from e
        return self.response_parser(proc.stdout, proc.stderr, proc.returncode)
