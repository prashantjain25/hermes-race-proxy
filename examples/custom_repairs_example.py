#!/usr/bin/env python3
"""
Example: plugging in your own repair strategy
================================================

This is a complete, runnable template for adding support for a NEW
vendor's failure shape to hermes-race-proxy, without editing
race_proxy_core.py or repairs.py at all.

Wire it up by pointing your proxy config at this file:

    {
      "custom_repairs_module": "/absolute/path/to/custom_repairs_example.py",
      "backends": [...]
    }

or in YAML:

    custom_repairs_module: /absolute/path/to/custom_repairs_example.py

The proxy loads this module at startup and calls its register() function,
which adds your strategy to the shared registry — every backend that
doesn't explicitly restrict its `repairs:` list picks it up automatically,
alongside the built-in structured-output and token-starvation repairs.

Run this file directly (`python3 custom_repairs_example.py`) for a
standalone demo of the strategy against fabricated request/response
pairs — no proxy or network needed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Make repairs.py importable when this file is run directly from the
# examples/ subdirectory (python3 examples/custom_repairs_example.py).
# Not needed if you import this module's register() from your own code
# with the repo root already on sys.path / PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repairs import RepairRegistry, RepairStrategy


class DropUnknownTopLevelFields(RepairStrategy):
    """Retry with a set of caller-supplied top-level fields stripped.

    Real-world motivating case: several OpenCode Go models were observed
    rejecting requests that included non-standard top-level fields like
    "mcp" or "system" (sent by some clients as extra metadata) with a
    strict-validator 400:

        "Error from provider: 2 request validation errors: Extra inputs
        are not permitted, field: 'mcp', value: [...]; Extra inputs are
        not permitted, field: 'system', value: '...'"

    This strategy demonstrates the general shape for "vendor X rejects
    field Y with error text Z" — the built-in StructuredOutputRelaxation
    in repairs.py is the same pattern applied to response_format
    specifically. Adjust FIELDS_TO_DROP and the applies() check for your
    own vendor's quirk.
    """

    name = "drop_unknown_fields"
    max_rungs = 1

    #: Fields to strip on retry. Edit this for your own vendor.
    FIELDS_TO_DROP = ("mcp", "system_metadata")

    def applies(self, body: dict, result: dict) -> bool:
        if result.get("ok"):
            return False
        if result.get("status_code") not in (400, 422):
            return False
        err = (result.get("error") or "").lower()
        # Only fire when the error text plausibly matches this vendor's
        # known rejection AND the request actually carries a field we
        # know how to drop — otherwise let other strategies (or nothing)
        # handle it.
        mentions_extra_fields = "extra inputs are not permitted" in err
        has_droppable_field = any(f in body for f in self.FIELDS_TO_DROP)
        return mentions_extra_fields and has_droppable_field

    def propose(self, body: dict, result: dict, rung: int) -> Optional[dict]:
        if rung != 1:
            return None
        new_body = dict(body)
        for field in self.FIELDS_TO_DROP:
            new_body.pop(field, None)
        return new_body

    # resolved() left at the RepairStrategy default (plain HTTP success)
    # — this strategy doesn't need TokenStarvationBoost's extra check.


def register(registry: RepairRegistry) -> None:
    """Entry point the proxy calls after loading this module.

    Add as many strategies as you like here — this is the ONLY function
    the loader looks for, and it's the only thing you need to define to
    plug in new repair behavior.
    """
    registry.register(DropUnknownTopLevelFields())


if __name__ == "__main__":
    # Standalone demo: exercise the strategy against a fabricated
    # failure, without needing a running proxy or a real network call.
    strategy = DropUnknownTopLevelFields()

    fake_body = {
        "model": "kimi-k2.6",
        "messages": [{"role": "user", "content": "hi"}],
        "mcp": ["context7", "serena"],
    }
    fake_result = {
        "ok": False,
        "status_code": 400,
        "error": "HTTP 400: Error from provider: 2 request validation "
                 "errors: Extra inputs are not permitted, field: 'mcp', "
                 "value: [...]",
    }

    print("applies():", strategy.applies(fake_body, fake_result))
    fixed = strategy.propose(fake_body, fake_result, rung=1)
    print("proposed retry body:", fixed)
    assert fixed is not None and "mcp" not in fixed
    print("OK — 'mcp' field dropped for retry.")
