# Contributing to hermes-race-proxy

Thanks for taking a look. This is a small, single-file tool on purpose, so
contributions that keep it that way are the easiest to merge.

## Getting started

1. Fork the repo and clone your fork.
2. No build step, nothing to install. Just run it:
   ```bash
   python3 race_proxy.py --config race_proxy.example.json --verbose
   ```
3. Make your change.
4. Test it against real backends (or mocked ones, see Testing below) before
   opening a PR. There's no CI yet, so what you show in the PR description
   is what I have to go on.

## What I care about here

I want this to stay small enough that anyone can read the whole thing in a
few minutes, so a few things I'll push back on:

- Adding a dependency you don't strictly need. PyYAML is optional and only
  matters if you want YAML configs; JSON has to keep working with nothing
  beyond the standard library.
- Splitting the single file into a package. It's meant to be easy to read
  top to bottom, and easy to just drop into another project.
- Undocumented tradeoffs. If your change adds a new limitation, put it in
  the README's "Things I haven't solved yet" section in the same PR. Don't
  make me find it later.
- Anything that phones home. This tool only ever talks to the backends you
  configure. That's not up for debate.

## What's genuinely welcome

- Bug fixes. A `curl` command showing expected vs. actual is enough of a
  repro.
- Streaming support (`stream: true` passthrough). This is the biggest gap
  right now and I'd like to see it fixed.
- An automated test suite, mocking the backend HTTP calls. There isn't one
  yet and it would make me a lot more comfortable merging changes.
- Doc fixes and clarifications.
- Better config validation, ideally catching a missing `backends` list or a
  malformed entry before the server even starts, instead of failing on the
  first request that comes in.

## Talk to me first (open an issue before a PR)

- Adding a required dependency.
- Adding auth to the proxy itself. Right now it's intentionally out of
  scope; this thing assumes you're running it somewhere you trust.
- Splitting the file up.
- Changing what counts as a "usable" response, or how a race gets decided.
  People may already be relying on the current behavior.

## Code style

- Standard library only, unless we've talked about it in an issue first.
- Type hints on public functions.
- Comments should explain why, not just what. The existing code tries to do
  this and new code should match.
- No real API keys, model names, or provider URLs anywhere in the repo,
  including examples. Use the generic placeholders already in
  `race_proxy.example.json`/`.yaml` (`backend-a`, `your-model-name`,
  `your-provider.example.com`) in docs and examples too.

## Testing

No automated suite yet (see above). Until there is one, please include in
your PR:

- The command you used to start the proxy.
- The exact `curl` (or similar) request you used to test the change.
- What you actually saw come back.

## Reporting security issues

This proxy has no built-in auth by design and is meant for local use only.
If you find a way it could leak configured credentials somewhere it
shouldn't (logs, error messages, an unintended request), open an issue
describing it. There's no dedicated security contact right now; a public
issue is fine given what this tool is and isn't meant to do.

## License

By contributing, you're agreeing your changes are licensed under this
project's MIT license (see [LICENSE](LICENSE)).
