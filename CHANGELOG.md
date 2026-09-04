# Changelog

All notable changes to this project are documented here. Dates are when the
change landed, not when it was designed.

## 1.1.0 — 2026-09-04

Toolset trimming ships. The `hermes` launcher now trims the eager tool-schema
payload off every main-model call, and the runtime config moves to YAML.

### Added
- `tool-trim.sh` launcher. Starts the race proxy (refcounted, torn down when the
  last session exits), then runs the real CLI (`hermes-real`) with a single `-t`
  union built from the live tool list plus `config/tools.yaml` overrides (`ON`
  forces a toolset in, `OFF` forces it out). A user-supplied `-t`/`--toolsets`
  bypasses the trimmer; every other flag passes through untouched. Wired as the
  thin-shim and zshrc-alias target, so the trim is on by default for every
  `hermes` invocation.
- `config/tools.yaml`: the categorized override catalog. Every CLI toolset with
  its `-t` flag, category, ON/OFF state, and the real tool names hermes registers
  (built-ins verbatim; MCP tools as `mcp__<server>__<tool>`, keyed by server name
  `exa`/`gemini-image`, since `-t "exa"` works on this install and
  `-t "mcp-exa"` does not).

### Changed
- `config/race_proxy.local.json` -> `config/race_proxy.local.yaml`. The reader
  (`load_config` in `race_proxy_core.py`) already spoke `.yaml`; the launcher now
  points at the YAML file, and the JSON is retired. Config is gitignored either way.
- Launcher renamed `race-proxy.sh` -> `tool-trim.sh` to match what it actually
  does (proxy lifecycle + toolset trim + flag passthrough). Thin shim, zshrc
  alias, and README updated; the superseded launcher and its backup were
  removed during the repo cleanup.

### Benchmark: input-token reduction on the main model (claude-sonnet-5)

Eager tool schemas dominate a cold prompt's input budget, and `-t` is exclusive,
so trimming toolsets is direct token savings on every main-model call. Measured
on the main model (claude-sonnet-5) with a `-z` one-shot through the shipped
wiring:

| Toolset union (`-t`) | Toolsets | Input (total) tokens | vs the 13-set default |
|---|---|---|---|
| default (exa + default-enabled) | 13 | 22,511 - 24,163 | baseline |
| trimmed: `delegation`/`cronjob`/`browser` OFF | 10 | 19,323 | -3,188 to -4,840 (14-20%) |
| minimal: `terminal` only (user bypass) | 1 | 4,815 | -17,696 to -19,348 (~79%) |

Read the numbers honestly:
- The default union's count drifts 22.5-24.2K across runs (system-prompt/env
  noise of a few hundred tokens); the trimmed value sat stable at 19,323 across
  two separate sessions.
- Single cold runs. The same `-t` union is provider-cached on repeat calls
  (`cache_read` on later runs), so steady-state cost is the marginal delta, not
  the full input.
- `browser` alone showed no reduction (capability-gated tools don't materialize
  schemas in a one-shot); the headline drop is `delegation` + `cronjob`.
- `terminal`-only is an illustration of the mechanism, not a usable daily config.

### Fixed
- Client-facing streaming responses. When a caller sends `stream: true`,
  the proxy now answers with proper Server-Sent-Events framing (one
  complete-content `delta` chunk, then `[DONE]`) instead of a flat
  `application/json` body. This proxy never streams token by token, it
  waits for the race to fully resolve before it has any answer at all, but
  a stream-decoding client still needs SSE framing on the wire to parse
  anything out of the response. Without this, a caller requesting
  `stream: true` would get a genuinely correct, complete answer back and
  silently decode it into nothing, because its stream parser found no SSE
  frames in a plain JSON body. Compaction and skills-hub calls that use
  streaming now work end to end; verified against the real `openai` Python
  SDK, not just raw HTTP.
- `usage` and `tool_calls` now pass through on the streamed response too,
  matching what a spec-compliant stream consumer reads off a
  `chat.completion.chunk`.

### Added
- `Backend.extra_body` (`race_proxy_core.py`): generic, provider-agnostic
  mechanism to merge fixed fields into every outbound request for a
  backend (e.g. a vendor's reasoning/thinking-budget knob). Core has no
  idea what any given key means, that's each provider's concern.
- `providers/http/gcp.py`: `build_gemini_31_flash_lite_backend()` now
  defaults to `reasoning_effort: minimal` on gemini-3.1-flash-lite,
  measured live at roughly 60-80% higher completion-token throughput
  than the default (multiple independent benchmark runs, both raw HTTP
  and through this repo's own Backend.call() path, see the file's
  docstring for exact numbers). Pass `extra_body={}` to disable.
- `providers/http/gcp.py`: Google Gemini via its OpenAI-compatible
  endpoint. Verified against this repo's own production compaction
  proxy log (race-proxy.log), which has been racing gemini-3.5-flash-lite
  on this exact base_url and winning repeatedly with real 200s.
- `providers/cli/hermes.py`: CLI-only backend for Hermes Agent's own
  CLI (`hermes -z "..."`), the CLI this repo is actually tested
  against. Fully content-verified end to end through Backend.call()
  (real "PING_OK" echoed back, including with --reasoning minimal),
  unlike claude.py/opencode.py's CLI backends which hit account-level
  auth blocks during their own live checks.
- `providers/cli/claude.py` and `providers/cli/opencode.py`: CLI-only
  backends for Claude Code's CLI and OpenCode's coding-agent CLI. Argv
  construction and error-shape handling verified live; full
  content-verified success not captured (blocked by account-level auth
  on this machine, not a wiring issue).
- `wire_format.py`: a small module that owns response wire-shaping only
  (JSON vs SSE framing for whoever is calling this proxy). Kept separate
  from `race_proxy_core.py` on purpose, same reasoning as the
  `providers/` and `callers/` split: the racing engine shouldn't carry
  knowledge of any particular caller's request/response conventions.
  Depends on nothing but the standard library.
- `examples/cli_caller_example.py`: runnable, key-free demo of the
  `Caller` interface (`callers/`), using `echo` as a stand-in CLI so it
  runs with zero setup.
- `examples/streaming_response_example.py`: runnable, no-network demo
  of `wire_format.py` showing the exact bytes sent for a `stream: true`
  vs `stream: false` request.

## 2026-09-02

### Fixed
- Upstream SSE leak causing 502s: the proxy forwarded a client's
  `stream: true` unmodified to the backend, so the backend replied with
  raw SSE text that `json.loads()` couldn't parse. Now forces
  `stream: false` on every outbound request to a backend, unconditionally.
- Fresh pooled HTTP connections skipped `settimeout()` because
  `conn.sock` was `None` before the first `.request()` call, so new
  connections silently fell back to the 15-second connect timeout instead
  of the configured read timeout. Connections are now forced open before
  the timeout is applied.

### Added
- `response_contracts.py`: an Adapter for normalizing whatever shape a
  backend's 200 response actually is into one canonical
  `chat.completion`-shaped dict. Includes reasoning-trace extraction that
  calls the real Hermes function when Hermes is importable, and falls
  back to a dated, clearly-marked field-alias mirror when it isn't.
  Nothing is pre-registered per vendor by default; a backend only gets a
  dedicated contract once a real, observed failure justifies writing one.
- `callers/` (Strategy pattern): separated "how to physically reach a
  backend" (HTTP socket today, a CLI subprocess for CLI-only vendors
  later) from `Backend` assembly, so a CLI-only vendor doesn't need
  changes to HTTP connection pooling code.
- `providers/http/` and `providers/cli/`: split the provider aggregator
  folder by transport. `providers/cli/` ships empty; it fills in only
  when a real CLI-only vendor gets wired up, not speculatively.
- `race_proxy_toolchain.py` and `race_proxy_compaction.py`: two thin
  entrypoints sharing `race_proxy_core.py`, so a long-running compaction
  call (300s timeout) can't hold connections and worker threads that a
  fast toolchain call (mcp, skills_hub, title_generation) needs.
  `race_proxy.py` stays as the original single-process entrypoint for
  anyone not running the split setup.

## 2026-08-30

### Added
- `providers/` layer: pluggable provider modules so a pool can be built
  from N providers x M models without writing a new backend loop for
  each one. Ships with an Ollama provider alongside the existing ones.
- Connection pooling, plus a split into core + pluggable repairs and
  discovery modules.
- Automatic structured-output and token-starvation repair: if a backend's
  response is truncated by a too-low `max_tokens`, retry once with a
  higher floor before giving up on that backend.

### Changed
- Restructured the README: table of contents, consolidated motivation and
  provider sections, collapsible deep-dives instead of one long scroll.

## 1.0.0 — 2026-08-29

### Added
- `CONTRIBUTING.md`.
- "How the parallelism works" section in the README, explaining the race
  concept plainly instead of assuming it's obvious.

### Initial release
- First commit: a local OpenAI-compatible HTTP proxy that fans one
  `/v1/chat/completions` request out to N upstream backends in parallel
  and returns whichever usable response comes back first.
