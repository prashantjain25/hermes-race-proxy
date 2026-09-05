# hermes-race-proxy

A zero-dependency, OpenAI-compatible local **LLM race proxy** for macOS and
Linux. It fans one `/v1/chat/completions` request out to several providers at
once and returns whichever answers first with a usable result — plus a launcher
that wires it into any agent CLI and trims the eager tool-schema payload off
every model call. Works with any OpenAI-compatible client (curl, LangChain, the
OpenAI SDK, agent frameworks); the [60-second install](#quickstart-60-seconds)
is a single `install.sh` with no runtime or toolchain to fetch on the target.
Designed for small machines (ARM64 SBCs and Apple Silicon alike) and for
low-memory installs where a lean Python runtime already exists.

**What problem it solves:** free/local LLM tiers are flaky — they 400, return
empty reasoning-budget answers, or flap between 200/503 minute to minute.
Racing several of them means one model's bad day never takes the whole pipeline
down, and a paid model's budget is held for calls that genuinely need it.

**Contents**
- [Features](#features)
- [Quickstart (60 seconds)](#quickstart-60-seconds)
- [How the race works](#how-the-race-works)
- [Why this exists](#why-this-exists)
- [Two proxies, one core: toolchain vs compaction](#two-proxies-one-core-toolchain-vs-compaction)
- [Project layout](#project-layout)
- [Runtime config (race-models.yaml + race_proxy_compaction.local.yaml)](#runtime-config-race-modelsyaml--race_proxy_compactionlocalyaml)
- [Was the response actually usable? (response contracts)](#was-the-response-actually-usable-response-contracts)
- [Reaching a backend: HTTP or CLI (callers)](#reaching-a-backend-http-or-cli-callers)
- [Providers and pooling (N providers x M models)](#providers-and-pooling-n-providers-x-m-models)
- [Built-in repairs (structured-output, token-starvation)](#built-in-repairs-structured-output-token-starvation)
- [Connection pooling](#connection-pooling)
- [Toolset trimming (hermes-warmup.sh)](#toolset-trimming-hermes-warmupsh)
- [Wiring it into Hermes Agent](#wiring-it-into-hermes-agent)
- [Config reference](#config-reference)
- [Extending: custom repairs / discovery / providers](#extending-custom-repairs--discovery--providers)
- [Known limitations](#known-limitations)
- [Contributing / License](#contributing--license)

## Features

The two halves of the repo serve different problems, so the features split the
same way.

**Racing responses.** One `/v1/chat/completions` request fans out to N backends
on N threads and the first *usable* answer wins, tagged with `_race_proxy.winner`
and `_race_proxy.latency`. Free/local models stay honest by load-sharing against
each other, one tier's bad day doesn't take the whole pipeline down, and a paid
model's budget is held for calls that actually need it. Response **contracts**
look inside a 200 and reject reasoning-budget starvation, safety-filter blocks,
and streaming leakage before an empty answer can win. Two failure modes are
**auto-repaired** without per-vendor error-string matching: structured-output
rejection (retry by dropping `strict`, then `response_format`) and token
starvation (retry with `max_tokens` raised to a safe floor). Providers, callers,
and startup discovery are **pluggable** — one file per vendor transport (HTTP or
CLI), a pool builder for N providers x M models, and a discovery policy you own.

**Live toolset trimming.** `hermes-warmup.sh` wraps `hermes` (the real CLI), starts
the race proxy (refcounted, torn down when the last session exits), and computes
a single `-t` union from hermes's own live tool list overridden by
`config/tools.yaml` (`state: ON` forces a toolset in, `OFF` forces it out).
Because `-t` is exclusive and eager tool schemas dominate a cold prompt, the trim
drops thousands of input tokens off every main-model call (around 20% at the
default union, ~80% trimmed to `terminal` alone; see the benchmark in the release
notes). A user passing their own `-t`/`--toolsets` bypasses the trimmer entirely.

Both halves are **update-immune**: `hermes-warmup.sh`, the proxy, and the configs all
live outside the `hermes-agent` git tree, so `hermes update` never modifies or
reverts them, and nothing in the agent's source is touched.

## Quickstart (60 seconds)

```bash
# 1 - install into ~/.hermes (creates a self-contained copy under HERMES_HOME,
#     generates the two per-machine proxy configs, wires the `hermes` shim
#     + shell alias). Nothing is fetched; the target needs no runtime installed.
git clone https://github.com/prashantjain25/hermes-race-proxy
cd hermes-race-proxy
./install.sh --commit

# 2 - in a new shell, the proxies start on first launch (refcounted)
hermes   # or, to start them standalone for a curl-only test:
~/.hermes/hermes-race-proxy/hermes-warmup.sh --version   # starts both, then returns

# 3 - confirm both are alive
curl -s http://127.0.0.1:8977/health   # compaction proxy: 300s timeout
curl -s http://127.0.0.1:8978/health   # toolchain proxy:  60s  timeout

# 4 - send a real chat request, watch who wins the race
curl -s -X POST http://127.0.0.1:8977/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say hi in 3 words"}],"max_tokens":1500}'
```

The installer provisions two proxy processes on two ports so a slow
context-compaction call can never starve a fast toolchain call:

- **compaction proxy** — `:8977`, 300s timeout, for large context-summary work.
- **toolchain proxy** — `:8978`, 60s timeout, for fast AI-agent tool calls.

Both are refcounted: they start once, are shared by every concurrent session,
and tear down when the last one exits. The installed tree under `~/.hermes` is
self-contained (same folder structure as the repo) and survives agent updates —
the proxy, launcher, and configs all live outside the agent's own install tree.

You get back a normal OpenAI-shape `chat.completion` object, plus one extra
field so you can see who won:

```json
{
  "...": "...",
  "_race_proxy": {"winner": "backend-a", "latency": 1.8}
}
```

## How the race works

1. A request comes in to `/v1/chat/completions`.
2. The proxy sends that same request to every configured backend at once, each on its own thread.
3. A response only counts if it has real content and, by default, `finish_reason: "stop"`. Some free-tier reasoning models burn their whole `max_tokens` budget on invisible reasoning and hand back an empty string, so the proxy treats that as a non-answer and keeps waiting on whoever else is still in flight.
4. First real answer wins, tagged with `_race_proxy.winner` and `_race_proxy.latency`.
5. Everyone else still running finishes on their own thread and gets thrown away; nothing waits on the losers.

Point it at any combination of paid, free, local, or hosted backends. Swap the placeholders in `examples/race_proxy.example.yaml` for your own providers and models.

## Why this exists

Hermes routes its auxiliary tasks (skill selection, MCP tool routing, approval checks, title generation, etc.) through `auxiliary.<task>.fallback_chain`, which tries backends **one at a time**: hit the primary, and only on failure/timeout does it move to the next. Fine for rare calls, annoying for the ones that fire on every turn, where you eat the primary's full timeout before the fallback gets a shot. This proxy fires the request at everything configured, simultaneously, and takes whoever answers first. Worst case you're bound by your slowest backend's timeout, not the sum of all of them.

<details>
<summary>Four concrete reasons this is a standalone proxy, not a Hermes core feature or PR</summary>

**Free models stop working the moment everyone finds out about them.** opencode.ai/zen's `-free` tier gets hammered by every agent framework that discovers it. Failures show up as an opaque 400 wrapped three layers deep, a silent empty response, or an endpoint flapping between 200 and 503 minute to minute. Racing several free/local/cheap-paid models means one model's bad day doesn't take the whole pipeline down, and spreads load instead of hammering one shared pool.

**Most auxiliary calls don't need your best model, they need an answer.** Title generation, skill routing, tool selection, approval checks: all small, frequent, low-stakes. Routing these through free/cheap/local models keeps your paid model's budget for turns that actually need it. In practice: `title_generation`, `skills_hub`, `mcp`, `approval`, `triage_specifier`, `kanban_decomposer`, `profile_describer`, `curator`, `monitor`, `memory_query_rewrite`, `goal_judge` all route through opencode.ai/zen's free tier; `compression` stays on a dedicated paid model since that call is quality-sensitive.

**One provider seeing your entire request history is a bigger blast radius than several providers each seeing a slice.** Splitting auxiliary traffic across providers (including a local model that never leaves the machine) means no single party holds the complete picture. It's not a defense against a targeted attacker, just less surface area for casual correlation.

**Getting an auxiliary call wrong costs more than the call itself.** A failed title_generation call that Hermes retries three times burns conversation-turn budget on what was supposed to be free overhead. Keeping the auxiliary layer working quietly is worth protecting on its own.

**Why not a PR into hermes-agent core instead:** Hermes's own contribution guidelines push capability to the edges and call new model tools "the expensive exception" (every tool ships on every API call). A parallel-racing proxy with pluggable repair/discovery/provider logic is plugin-shaped, not core-tool-shaped. It just needs `provider: custom, base_url: http://127.0.0.1:PORT/v1`, a shape Hermes already supports. It's also more durable: `~/.hermes/hermes-agent` is a live git checkout that `hermes update` pulls against directly, so anything merged/patched into that tree risks being overwritten. A standalone process talked to over `provider: custom` never touches that tree, so it survives every update by construction. And it isn't Hermes-specific: any OpenAI-compatible client (curl, LangChain, a homegrown script) benefits the same way.

A lighter-weight path back into the ecosystem later (a documentation skill, or a thin MCP server wrapper) both fit Hermes's "extend at the edges" model. Folding the racing/repair/discovery logic itself into `agent/auxiliary_client.py` does not.
</details>

## Two proxies, one core: toolchain vs compaction

Hermes routes several different auxiliary tasks through this proxy: context compression, MCP tool routing, skills-hub lookups, title generation. They are not the same kind of call. A compaction request can legitimately take minutes (a real run against opencode.ai/zen's `nemotron-3.5-lightning-free` took 702.84 seconds to come back with a valid, non-empty answer). A title-generation call should come back in a few seconds or not at all, because Hermes is waiting on it synchronously before it can respond to you.

Pointing every task at one shared process meant they all fought over the same connection pool, the same worker threads, and the same `timeout` value. A slow compaction attempt could hold resources a fast toolchain call needed, and a timeout generous enough for compaction (300s) was much too generous for a call that should fail fast and hand off to Hermes's own fallback chain instead.

The fix is two thin entrypoints over the exact same core logic, not two codebases:

```bash
python3 race_proxy_compaction.py --config race_proxy_compaction.local.yaml # port 8977, long timeout
python3 race_proxy_toolchain.py  --config race_proxy_toolchain.local.yaml # port 8978, short timeout
```

`race_proxy_compaction.py` keeps the original port (8977), so an existing `auxiliary.compression` config entry needs no changes. Point `auxiliary.mcp`, `auxiliary.skills_hub`, and `auxiliary.title_generation` at `race_proxy_toolchain.py`'s port instead:

```yaml
auxiliary:
  compression:
    provider: custom
    base_url: http://127.0.0.1:8977/v1   # unchanged
  skills_hub:
    provider: custom
    base_url: http://127.0.0.1:8978/v1   # moved to the toolchain proxy
  mcp:
    provider: custom
    base_url: http://127.0.0.1:8978/v1
  title_generation:
    provider: custom
    base_url: http://127.0.0.1:8978/v1
```

Nothing about the split changes what `race_proxy_core.py` does; it's the same racing/repair/contract logic served on two ports with two default timeouts instead of one.

## Project layout

| File | What it is |
|---|---|
| `install.sh` | Single-command installer: copies the repo's runtime files into `HERMES_HOME` (default `~/.hermes/hermes-race-proxy/`), generates the two per-machine configs, wires a `hermes` shim + shell alias. Prunes files no longer tracked and installs only `README.md` from the docs. Works on macOS + Linux, Bash + Zsh. |
| `hermes-warmup.sh` | The launcher every `hermes` invocation runs through: starts both proxies (refcounted, torn down at last exit), syncs MCP enable/disable from `config/tools.yaml`, builds the trimmed `-t` toolset union, then runs the real CLI. |
| `race_proxy_compaction.py` | Compaction entrypoint: long timeout, port 8977. Config is `config/race_proxy_compaction.local.yaml`. |
| `race_proxy_toolchain.py` | Toolchain entrypoint: short timeout, port 8978. Config is `config/race_proxy_toolchain.local.yaml`. |
| `config/` | Runtime config versioned with the code: `race-models.yaml` (models source of truth), `tools.yaml` (single tool/MCP policy), and the two generated `*.local.yaml` listener/timeout files. Secret keys never live here. |
| `race_proxy_core.py` | Backend/race mechanics: assembling a request, racing backends via their configured `caller`, serving the endpoint. Knows nothing about *why* a response might be broken. |
| `repairs.py` | *Why* a response might be broken and how to fix it, behind a `RepairStrategy` interface. |
| `response_contracts.py` | *Was* a 200 response actually usable, per backend, behind a `ProviderContract` interface. See [Was the response actually usable?](#was-the-response-actually-usable-response-contracts) below. |
| `callers/` | *How* to reach a backend at all: pooled HTTP by default (`callers/http_caller.py`), or a CLI subprocess for a vendor with no HTTP API (`callers/cli_caller.py`). See [Reaching a backend](#reaching-a-backend-http-or-cli-callers) below. |
| `discovery.py` | Optional backend-selection extension point: probe/rank candidate models at proxy startup instead of hardcoding a static list. Off by default. |
| `connection_pool.py` | HTTP connection pooling (persistent per-backend connections) and the shared worker thread pool. |
| `providers/base.py`, `providers/pool.py` | Convenience layer for `discovery.py`, shared across every provider: the `Provider` base contract and the N-providers x M-models pool builder. |
| `providers/http/` | One file per HTTP-reachable vendor's connection contract (opencode.ai/zen, OpenRouter, DeepInfra, NVIDIA build.nvidia.com, Ollama). |
| `providers/cli/` | CLI-only vendors (no HTTP API at all): `claude.py` (Claude Code CLI), `opencode.py` (OpenCode coding-agent CLI, a different product from the HTTP `opencode.py` above despite the name overlap), and `hermes.py` (Hermes Agent's own CLI, the one this repo is actually tested against day to day). |
| `examples/custom_repairs_example.py` | Runnable template for a custom repair strategy. |
| `examples/custom_discovery_example.py` | Runnable template for a custom discovery policy, hand-rolled without `providers/`. |
| `examples/provider_pool_example.py` | The same policy rewritten on `providers/`, the recommended starting point once you want more than one provider. |
| `examples/cli_caller_example.py` | Runnable demo of the `Caller` interface (`callers/`) using `echo` as a stand-in CLI, no real vendor or key needed. |
| `examples/streaming_response_example.py` | Runnable, no-network demo of `wire_format.py`: exactly what bytes go out for a `stream: true` vs `stream: false` request. |

Default behavior with none of the extension points configured is an unchanged plain static `backends:` list in config; everything above is opt-in.

## Runtime config (race-models.yaml + race_proxy_compaction.local.yaml)

The model lineup is declared once in `config/race-models.yaml`, anchors-first: top-level `intents:` names what the proxy is for, providers are sub-keys under each, and a provider's endpoint lives exactly once under `providers:`.

```yaml
# config/race-models.yaml
providers:
  opencode:                     # keyless free tier — no key at all
    base_url: https://opencode.ai/zen/v1
    api_key: ""
    headers: {}
  gemini:
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
    api_key_ref: gemini         # resolved at load time, never stored here

intents:
  compaction:                   # context-compression traffic
    gemini:
      - model: gemini-3.1-flash-lite
        extra_body: { reasoning_effort: minimal }
      - gemini-3.5-flash-lite
    opencode:
      - nemotron-3.5-lightning-free
  toolchain_mcp:                # mcp / skills_hub traffic
    opencode:
      - laguna-s-2.1-free
      - nemotron-3.5-lightning-free
```

`config/race_proxy_compaction.local.yaml` is the listener/timeout file that points at it. Relative `models_file`/`secrets_file` paths resolve against the config file's own directory, so the whole `config/` can be cloned or moved anywhere:

```yaml
host: 127.0.0.1
port: 8977
timeout: 300
require_finish_reason: stop
models_file: race-models.yaml
pool_file: ~/.hermes/auth.json
```

Every backend produced from the anchors above binds to its provider's endpoint, deduped by `(model, endpoint)` so a model shared across anchors isn't raced twice. `intents` anchors and `providers` can be split into separate concerns cleanly (e.g. a provider registry that `alias_of`-shares one endpoint under several names).

**API keys are never committed.** A provider points at a key by name (`api_key_ref: gemini`) and the proxy resolves it at load time, in order: the credential pool (`~/.hermes/auth.json` → `credential_pool.<provider>`, seeded with `hermes auth add <provider> --type api-key --api-key <KEY>`), else a local `secrets_file` map, else `api_key_env` from an environment variable. `race_proxy_compaction.local.yaml` is gitignored; commit only the secret-free `race-models.yaml` and this README.

## Was the response actually usable? (response contracts)

An HTTP 200 is not the same claim as "this backend actually answered." A 200 can wrap Server-Sent-Events text instead of one JSON object (seen live: a `stream` flag leaking through to a backend that doesn't buffer streaming responses), a reasoning model that spent its whole token budget thinking and came back with empty `content`, or a response blocked by a safety filter. None of that shows up in the status line.

`response_contracts.py` is the layer that looks inside a 200 and decides if it's real. `GenericOpenAIContract` is the one contract almost every backend uses: it already handles reasoning-budget starvation (checking several field-name conventions across vendors, reusing Hermes's own `extract_content_or_reasoning()` logic when this proxy is running inside a Hermes checkout, so the list of known reasoning-field names never drifts out of sync with what Hermes itself already knows), safety/content-filter blocks, and streaming leakage, as generic checks that don't assume anything vendor-specific.

Nothing is pre-registered per vendor by default. A new `ProviderContract` gets written only when a real, observed response shape proves the generic one can't express it, the same reactive posture as adding a new repair strategy: build it against verified behavior, not a guess at what a vendor's interface might look like someday.

## Reaching a backend: HTTP or CLI (callers)

Separate from whether a response was usable is how the proxy fetches it in the first place. Every backend in this repo talks HTTP today, pooled through `connection_pool.py`, but not every LLM vendor exposes one. Some ship an official CLI instead and nothing else, no documented HTTP contract a third party can build against.

`callers/` is the interface that makes that swap possible without touching anything else. `HttpCaller` (the default) wraps the existing pooled-HTTP logic unchanged. `CliCaller` runs a vendor's official CLI as a subprocess, feeds it the request on stdin, and reads its stdout back, returning the exact same `(status, raw_bytes)` shape either way, so `Backend`, the race logic, and the response-contract layer never need to know or care which caller answered.

Config-driven, per backend:

```json
{
  "name": "cli-vendor",
  "caller": "cli",
  "command": ["some-vendor-cli", "chat", "--stdin"],
  "model": "their-model-name"
}
```

Nothing here is speculative. No CLI-only vendor ships in this repo yet, `providers/cli/` stays empty until one is actually wired up and verified against the real CLI's real behavior. See `examples/cli_caller_example.py` for a complete, runnable demo of the `Caller` interface itself (using `echo` as a stand-in CLI, no real vendor or key needed to see how it wires together).

## Providers and pooling (N providers x M models)

`providers/` factors each vendor's connection contract (base_url, auth style, model-listing shape) into one small object, and `providers/pool.py` turns "N enabled providers, each contributing M models" into one flat backend list:

    total backends = sum(top_n for each enabled ProviderSlot)

```python
from providers.http.opencode import OpenCodeZenProvider
from providers.http.nvidia_build import NvidiaBuildProvider
from providers.pool import ProviderSlot, build_pool

def discover_backends(cfg: dict):
    return build_pool([
        # Fixed: exact models, no probing.
        ProviderSlot(
            provider=OpenCodeZenProvider(),
            model_ids=["nemotron-3.5-lightning-free", "laguna-s-2.1-free"],
        ),
        # Discovered: exhaustively probe, keep the top 2 fastest.
        ProviderSlot(
            provider=NvidiaBuildProvider(),
            api_key=cfg.get("nvidia_api_key", ""),
            top_n=2,
            candidate_prefixes=("deepseek-ai/", "nvidia/", "meta/", "qwen/"),
        ),
    ])
```

See `examples/provider_pool_example.py` for this exact policy as a complete `custom_discovery_module`, verified end-to-end against live opencode.ai/zen and build.nvidia.com endpoints.

### HTTP providers

| Provider | File | base_url | Needs API key? |
|---|---|---|---|
| opencode.ai/zen | `opencode.py` | `https://opencode.ai/zen/v1` | Nemotron/Laguna: no. GLM/Kimi: yes |
| Google Gemini | `gcp.py` | `https://generativelanguage.googleapis.com/v1beta/openai` | Yes, always |
| OpenRouter | `openrouter.py` | `https://openrouter.ai/api/v1` | Yes (401 keyless, even `:free` models) |
| DeepInfra | `deepinfra.py` | `https://api.deepinfra.com/v1/openai` | Yes (401 keyless) |
| NVIDIA build.nvidia.com | `nvidia_build.py` | `https://integrate.api.nvidia.com/v1` | Yes (trial credits, not a stable free tier) |
| Ollama (local) | `ollama.py` | `http://localhost:11434/v1` | No (dummy key, per Ollama's own convention) |

`opencode.py` covers the whole opencode.ai/zen platform, not one model: `build_nemotron_backend()`, `build_laguna_backend()`, `build_glm_backend(api_key)`, `build_kimi_backend(api_key)`.

Live-tested against the real vendor: opencode.ai/zen, OpenRouter, DeepInfra, NVIDIA, GCP Gemini. **Ollama is config-only, not live-verified.**

### CLI providers

| CLI | File | Content-verified? |
|---|---|---|
| Hermes Agent (`hermes -z`) | `hermes.py` | Yes, real `PING_OK` echoed back through `Backend.call()`, plain and with `--reasoning minimal` |
| Claude Code (`claude -p`) | `claude.py` | No, argv verified only, blocked by an expired OAuth session |
| OpenCode CLI (`opencode run`) | `opencode.py` | No, argv verified only, blocked by an account credit limit |

`providers/cli/opencode.py` is OpenCode's coding-agent CLI, a different product from `providers/http/opencode.py` (the opencode.ai/zen HTTP platform) despite the shared name.

### Tested models

Real runs against the real vendor, not spec-sheet claims. "avg" = mean of 3 runs.

| Model | Where tested | Result |
|---|---|---|
| `gemini-3.5-flash-lite` | Production `race-proxy.log` | 200 OK repeatedly, wins races; 2.89s-10.40s per win |
| `gemini-3.1-flash-lite`, default | Raw HTTP | 200 OK; avg 87.90 tok/s |
| `gemini-3.1-flash-lite`, `reasoning_effort: minimal` | Raw HTTP | 200 OK; avg 143.94 tok/s |
| `gemini-3.1-flash-lite`, `reasoning_effort: minimal` | Full proxy stack (`Backend.call()`, real config, real port) | 200 OK, `race-done ok=True`; avg 157.61 tok/s |
| `gemini-2.5-flash-lite` | Raw HTTP + `Backend.call()` | **Retired.** Real HTTP 404 |
| `nemotron-3.5-lightning-free` | Production `race-proxy.log` | Timed out at 300s once observed |
| `laguna-s-2.1-free` | Production `race-proxy.log` | 200 OK but slow: 91.74s-134.24s |
| `hermes-cli` | `hermes -z`, via `Backend.call()` | 200 OK, real `PING_OK`; not tok/s-benchmarked (CLI startup dominates) |
| `claude` CLI | subprocess | Blocked: expired OAuth session |
| OpenCode CLI | subprocess | Blocked: account credit limit |

<details>
<summary>Add your own provider (vLLM, LM Studio, Groq, Together, Fireworks, ...)</summary>

One new file subclassing `Provider`:

```python
from providers.base import Provider

class MyProvider(Provider):
    name = "my-provider"
    base_url = "https://api.my-provider.example.com/v1"
    requires_api_key = True  # or False for a keyless/local endpoint

    def default_headers(self) -> dict:
        return {}  # anything beyond Authorization this vendor wants
```

`list_models()` works unmodified if the vendor's `/v1/models` returns the standard `{"data": [{"id": "..."}]}` shape (most do); override it if not. Drop the file in `providers/http/`, add a `ProviderSlot` for it in your discovery script, and it races alongside everything else configured, cloud and local together, with the same structured-output and token-starvation repairs applying automatically (they live in `repairs.py`, not per-provider).

PRs adding a new provider file are welcome, with the same "verified live, here's how" discipline the four cloud providers follow.
</details>

## Built-in repairs (structured-output, token-starvation)

Two failure modes hit real usage and get auto-repaired without needing to detect the vendor's specific error string:

1. **Structured-output rejection.** A `response_format: {"type": "json_schema", "strict": true, ...}` request 400s/422s at gateways that advertise Chat Completions compatibility without implementing strict-schema enforcement (e.g. opencode.ai/zen fronting `nemotron-3.5-lightning-free`, `laguna-s-2.1-free`). The proxy retries with a fixed ladder: (1) original request, (2) drop `strict: true`, keep the schema, (3) drop `response_format` entirely (caller needs a loose-JSON-extraction fallback for this rung; Hermes's own `title_generator._extract_title_text` already has one).
2. **Token starvation.** Free-tier reasoning models can return `200 OK` with empty content and `finish_reason: "length"` because the entire `max_tokens` budget went to hidden reasoning before any visible output. The proxy detects that exact shape (empty content **and** `finish_reason: "length"`) and retries once with `max_tokens` raised to a safe floor (`MIN_SAFE_MAX_TOKENS`, 2000 by default).

Both repairs are request/response-shape driven. Neither cares which vendor produced the failure, so the same ladder applies to any OpenAI-compatible backend without code changes. That portability is also why this lives in the proxy instead of Hermes: Hermes's own `_is_structured_output_rejection` detector in `agent/auxiliary_client.py` only fires on known error-text substrings, so it's always one new vendor behind, and that fix lives inside `hermes-agent`'s own git tree where `hermes update` can overwrite a local patch.

Turn either repair off per-backend if you'd rather see the raw failure:

```json
{
  "name": "backend-a",
  "base_url": "https://your-provider.example.com/v1",
  "model": "your-model",
  "api_key": "",
  "repair_structured_output": true,
  "repair_token_starvation": true
}
```

<details>
<summary>Reproduction details and verification evidence</summary>

The opaque 400 that triggered this, verbatim:

```
Error code: 400 - {"error": {"type": "server_error", "message":
"Error from provider (Console): Upstream request failed: [400] Provider
returned error"}}
```

No mention of `response_format`, `json_schema`, or `strict`, just a generic wrapper. NVIDIA's own Nemotron docs recommend loose `json_object` mode instead of strict `json_schema` for the same reason; an OpenRouter `ai-sdk-provider` issue (#483) documents the identical failure shape for a different vendor entirely.

Verified end-to-end: a strict-`json_schema` request with `max_tokens: 64` (Hermes's actual `title_generator.py` default) against `nemotron-3.5-lightning-free`, which 400s on the original request every time. Through the proxy: 8/8 trials returned a correct, schema-shaped title with `finish_reason: "stop"`, each via two stacked repairs (`response_format` dropped, then `max_tokens` boosted), tagged `"_race_proxy": {"repaired_rung": "format:2+tokens", ...}`.
</details>

## Connection pooling

Every backend gets a small pool of persistent HTTP connections (`connection_pool.py`) instead of a fresh TCP+TLS handshake per call, the same pattern a database client uses: pay setup cost once, reuse the connection, replace it transparently if the far end silently closed it while idle. The race thread pool is likewise created once at startup and reused for the process lifetime.

Pool stats are exposed on the health endpoint:

```bash
curl -s http://127.0.0.1:8977/health | python3 -m json.tool
# {
#   "status": "ok",
#   "backends": ["nemotron", "laguna"],
#   "timeout": 90,
#   "connection_pools": [
#     {"host": "opencode.ai", "port": 443, "in_use": 1, "idle": 2, "max_size": 8}
#   ]
# }
```

## Toolset trimming (hermes-warmup.sh)

`hermes-warmup.sh` is the launcher that wraps the real CLI (`hermes-real`): it keeps
the race proxy up (refcounted, torn down when the last session exits) and then
runs `hermes`. Before it does, it computes a single `-t` union for the call:

    base     = live enabled toolsets + MCP servers, from `hermes tools list`
    override = config/tools.yaml   (state: ON -> force in,  OFF -> force out)
    result   = (base + forced-in) - forced-out, one sorted, comma-joined -t flag

Because `-t` is exclusive, the union always starts from hermes's own live list.
That keeps it update-immune in both directions: `hermes update` auto-adds new
toolsets with hermes's default state (the trim never has to chase the catalog),
and the YAML only grows when you want a state that differs from a default. A
`state: OFF` row never reaches the flag; flip a row to `ON` and it is force-in.
Toolsets are the control unit — `state` toggles a whole toolset, not individual
tools — and MCP servers are keyed by their server name (`exa`), not the docs'
`mcp-<server>` form (on this install `-t "exa"` works, `-t "mcp-exa"` does not).
A user-supplied `-t`/`--toolsets` bypasses the YAML entirely.

The payoff is on the main model's cold prompt, where eager tool schemas dominate
the input budget. Measured on `claude-sonnet-5`: the default 13-toolset union is
around 22.5-24K input tokens, trimming the non-essential `delegation`/`cronjob`/
`browser` toolsets drops it to ~19.3K, and a `terminal`-only set is ~4.8K. See
the full benchmark table in [CHANGELOG.md](CHANGELOG.md).

## Wiring it into Hermes Agent

Point an auxiliary task's `base_url` at the proxy instead of the real provider:

```yaml
auxiliary:
  skills_hub:
    provider: custom
    base_url: http://127.0.0.1:8977/v1
    model: race-proxy   # ignored, the proxy swaps in each backend's real
                          # model name from its own config
```

Start the proxy before starting Hermes, and let your process supervisor of choice (systemd, launchd, pm2, whatever) keep it running.

Alternatively, wrap the real CLI with `hermes-warmup.sh` so a single daemon is shared across interactive shells and background processes — it starts the proxy on the first invocation, builds a trimmed `-t` toolset union from the live tool list plus `config/tools.yaml` overrides, and tears the proxy down when the last one exits (refcounted), passing every CLI arg through untouched (a user-supplied `-t`/`--toolsets` bypasses the trimmer):

```bash
# ~/.local/bin/hermes  →  thin shim (install.sh creates this symlink):
exec "$HERMES_HOME/hermes-race-proxy/hermes-warmup.sh" "$@"
# alias hermes="$HERMES_HOME/hermes-race-proxy/hermes-warmup.sh"   (in your shell rc)
```

The real CLI, this repo, and `hermes-warmup.sh` all live outside the agent's own install tree, so an agent update (which pulls that tree) never touches them — nothing in the agent's source is modified, so there's nothing to revert before an update, and the hook always survives it.

Compaction calls in particular send `stream: true` when Hermes has a progress hook active on that call. The proxy answers with a single SSE `delta` chunk carrying the complete response, not a real token-by-token stream, but enough for Hermes's own stream decoder to read real content back instead of aggregating an empty string from a response it couldn't parse as a stream. Verified end to end against the real `openai` Python SDK against a live compaction-shaped request; see [CHANGELOG.md](CHANGELOG.md) for the specific failure this replaced. See `examples/streaming_response_example.py` for a standalone, no-network demo of exactly what bytes go out on the wire either way.

## Config reference

```json
{
  "host": "127.0.0.1",
  "port": 8977,
  "timeout": 90,
  "require_finish_reason": "stop",
  "backends": [
    {
      "name": "backend-a",
      "base_url": "https://your-provider.example.com/v1",
      "model": "your-model-name",
      "api_key": "",
      "headers": {},
      "repair_structured_output": true,
      "repair_token_starvation": true
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `host` / `port` | Where the proxy's HTTP server listens. |
| `timeout` | How long a single race runs before giving up and returning `502`. Each backend's internal repair retries share this budget, and with both repairs enabled a backend can make up to 4 HTTP attempts per incoming request, so don't set this too low. 60-90s is reasonable. |
| `require_finish_reason` | Set `null`/omit to accept truncated completions. Default `"stop"` rejects `finish_reason: "length"` results (the usual failure mode when a reasoning model gets too small a `max_tokens`). |
| `backends[].api_key` | An empty string sends an explicit empty `Authorization` header. Some keyless free tiers 401 you the moment they see any recognized bearer-token format. |
| `backends[].repair_structured_output` | Default `true`. See "Built-in repairs" above. |
| `backends[].repair_token_starvation` | Default `true`. Retries once with `max_tokens` raised to `MIN_SAFE_MAX_TOKENS` (2000; edit the constant in `race_proxy_compaction.py`/`race_proxy_core.py` for a different floor). |
| `backends[].caller` | `"http"` (default) or `"cli"`. See [Reaching a backend](#reaching-a-backend-http-or-cli-callers) above. |
| `backends[].command` | Only used when `caller: "cli"`. Argv list for the vendor's official CLI. |
| `models_file` | Path to `race-models.yaml` (resolved relative to this config file). When set and no inline `backends:` is present, every model under the `intents:` anchors becomes the raced backends. |
| `pool_file` | Credential-pool JSON used to resolve any `api_key_ref`. Hermes default is `~/.hermes/auth.json` → `credential_pool.<provider>`. |
| `secrets_file` | Optional YAML/JSON map of `ref: key` for providers not covered by the pool. |
| `intent` | Optional — expand only this one anchor from `models_file` instead of all anchors. |

## Extending: custom repairs / discovery / providers

**Custom repair strategy.** Hit a different vendor's failure shape (rejected field, truncation pattern)? No fork/PR needed:
1. Write a standalone `.py` file. Subclass `RepairStrategy` from `repairs.py`, implement `applies()` and `propose()`.
2. Define `register(registry) -> None` calling `registry.register(YourStrategy())`.
3. Point config at it: `"custom_repairs_module": "/path/to/my_repairs.py"`.

See `examples/custom_repairs_example.py` (dropping unknown top-level fields some vendors reject with a strict-validator 400).

**Custom discovery policy.** Which models to race, how many, from which providers, and how to rank them at startup is a personal policy call:
1. Write a standalone `.py` file defining `discover_backends(cfg: dict) -> list[Backend]`.
2. Point config at it: `"custom_discovery_module": "/path/to/my_discovery.py"`.
3. Runs once at proxy startup, not per-request. An exhaustive probe of a dozen candidate models to pick the fastest few is reasonable here since it never adds latency to a real chat-completion call later.
4. A module that fails to load, raises, or returns nothing degrades to the static `backends:` list with a logged warning, never to "no backends at all."

See `examples/custom_discovery_example.py` (2 fixed backends plus the top 2 fastest of an exhaustive startup probe, candidates run in parallel).

**Custom provider.** See "Add your own provider" under [Providers and pooling](#providers-and-pooling-n-providers-x-m-models) above.

## Known limitations

- **You're now sending 2x, 3x, however many requests you're racing.** If backends share a rate limit, racing burns through it faster. The structured-output/token-starvation repairs multiply this further (up to 4 attempts per request on a backend needing both), free on keyless tiers but worth knowing if paying per token.
- **No caching.** Every request races from a cold start.
- **No true token-by-token streaming.** The race always waits for a full response before it has an answer at all, there's no way around that when the whole point is comparing N complete responses against each other. What changed: a client that requests `stream: true` now gets that complete answer back over proper SSE framing (one `delta` chunk, then `[DONE]`) instead of a flat JSON body, so streaming-aware clients decode it correctly instead of silently seeing empty content. The wire-shaping for this lives in `wire_format.py`, kept separate from the racing logic on purpose.
- **No auth on the proxy itself.** Meant to live on localhost. Put a real reverse proxy with real auth in front if exposing it.
- **The token-starvation floor is a single global constant, not per-model.** `MIN_SAFE_MAX_TOKENS` (2000) applies across all backends; raise it if a heavy-reasoning-overhead model still starves.
- **Trial/credit-limited discovery APIs need your own production judgment.** The example discovery script probes NVIDIA's build.nvidia.com catalog, an explicit TRIAL service under NVIDIA's API Trial Terms (credit-limited, ~40 RPM account-wide, undocumented, not licensed for production traffic). Treat any trial/free-tier discovered backend as best-effort supplementary, racing alongside more predictable fixed backends, not alone.
- **No automated test suite yet.** Tested by hand against live free-tier endpoints; treat accordingly.

## Contributing / License

See [CONTRIBUTING.md](CONTRIBUTING.md). See [CHANGELOG.md](CHANGELOG.md) for release history and [CONTRIBUTORS.md](CONTRIBUTORS.md) for who's built this. MIT licensed.
