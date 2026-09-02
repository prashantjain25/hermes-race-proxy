#!/usr/bin/env python3
"""
hermes-race-proxy: CLI-only vendor aggregators
==================================================

Empty by design, same reasoning as ``response_contracts.py``'s
"reactive not speculative" contract registry: this directory holds
concrete :class:`~providers.base.Provider` subclasses for vendors
reachable ONLY through their official CLI (no public HTTP chat-
completions API at all) — the devpass case, when it's actually added.

Do not pre-populate this directory with placeholder modules for
vendors that might get a CLI-only integration someday. A real entry
here gets written when a real CLI-only vendor is being wired up, the
same restraint ``response_contracts.ProviderContract``'s "Escape
hatch" section applies to writing a new response contract: build it
against the vendor's REAL, VERIFIED CLI behavior (see that module's
"MANDATORY BEFORE ADDING OR CHANGING A VERSION" guidance for what
"verified" means when a reseller or wrapper CLI sits between you and
the actual model), not a guess at what its interface might look like.

A CLI-only ``Provider`` subclass here pairs with a
``callers.cli_caller.CliCaller`` (see ``callers/cli_caller.py``) the
same way an HTTP provider in ``providers/http/`` pairs with
``callers.http_caller.HttpCaller`` — ``providers/base.py``'s
``Provider.build_backend()`` would need a small override in a CLI
subclass to construct its ``Backend`` with a ``CliCaller`` instead of
the base class's implicit HTTP assumption; there is no such override
yet because there is no CLI-only provider yet to write one for.
"""
