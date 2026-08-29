# Contributing to hermes-race-proxy

Thanks for considering a contribution. This is a small, single-file tool by
design, so contributions that keep it that way are especially welcome.

## Getting started

1. Fork the repo and clone your fork.
2. No build step and no required dependencies. Run it directly:
   ```bash
   python3 race_proxy.py --config race_proxy.example.json --verbose
   ```
3. Make your change.
4. Test it manually against real backends (or mocked ones, see "Testing"
   below) before opening a PR. There is no CI yet, so manual verification in
   the PR description matters.

## Project philosophy

- **Zero required dependencies stays non-negotiable.** PyYAML is optional
  and only needed for YAML configs; JSON config must keep working with
  nothing beyond the Python 3 standard library. Do not add a hard dependency
  without discussing it in an issue first.
- **Single file is a feature, not a limitation.** `race_proxy.py` is meant
  to be readable top to bottom in a few minutes and easy to vendor into
  another project. Prefer keeping it that way over splitting into a package,
  unless a change genuinely cannot be done cleanly otherwise.
- **Honest documentation over marketing.** If a change introduces a new
  limitation or tradeoff, document it in the README's "Known limitations"
  section in the same PR. Do not let a caveat go undocumented.
- **No telemetry, no phone-home behavior, ever.** This tool proxies your
  requests to backends you configure. It must never contact anything else.

## What's welcome

- Bug fixes with a clear repro (a `curl` command and expected vs. actual
  response is enough).
- Streaming support (`stream: true` passthrough), currently unsupported and
  flagged as a known limitation.
- An automated test suite (pytest, mocking the backend HTTP calls); there
  isn't one yet and it would meaningfully raise confidence in changes.
- Documentation fixes and clarifications.
- Additional config validation with clear error messages (e.g. catching a
  missing `backends` list or malformed entry before the server starts,
  rather than failing on the first request).

## What to discuss first (open an issue before a PR)

- Adding a required dependency.
- Adding authentication/authorization to the proxy itself (currently
  intentionally out of scope; the tool assumes a trusted local network).
- Splitting the single file into multiple modules.
- Any change to the race semantics (what counts as a "usable" response, how
  ties are broken, etc.) since other users may depend on the current
  behavior.

## Code style

- Standard library only unless explicitly agreed via an issue.
- Type hints on public functions.
- Keep comments that explain *why*, not just *what*; the codebase already
  does this and new code should match.
- No credentials, API keys, model names, or endpoint URLs specific to any
  one provider should be hardcoded anywhere in the repo, including examples.
  Use `race_proxy.example.json`/`.yaml` with generic placeholder names
  (`backend-a`, `your-model-name`, `your-provider.example.com`) for all
  documentation and examples.

## Testing

There is no automated test suite yet (see "What's welcome" above). Until one
exists, please include in your PR description:

- The exact command you ran to start the proxy.
- The exact `curl` (or equivalent) request(s) you used to verify the change.
- The relevant response output, or a description of the observed behavior.

## Reporting security issues

This tool has no built-in authentication by design and is meant for local
use only (see README's "Known limitations"). If you find a way the proxy
could leak configured credentials to an unintended party (e.g. via logs,
error messages, or a request it shouldn't be able to make), please open an
issue describing it. There is no dedicated security contact at this time;
public issues are fine for this project's threat model.

## License

By contributing, you agree your contributions are licensed under the
project's MIT license (see [LICENSE](LICENSE)).
