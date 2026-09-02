# hermes-race-proxy

A tiny, zero-dependency local HTTP proxy that races a few OpenAI-compatible
LLM backends against each other and hands back whichever one answers first
with something usable. Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
auxiliary-task routing, but works with any OpenAI-compatible client.

**Contents**
- [Quickstart](#quickstart-60-seconds)
- [How the race works](#how-the-race-works)
- [Why this exists](#why-this-exists)
- [Two proxies, one core: toolchain vs compaction](#two-proxies-one-core-toolchain-vs-compaction)
- [Project layout](#project-layout)
- [Was the response actually usable? (response contracts)](#was-the-response-actually-usable-response-contracts)
- [Reaching a backend: HTTP or CLI (callers)](#reaching-a-backend-http-or-cli-callers)
- [Providers and pooling (N providers x M models)](#providers-and-pooling-n-providers-x-m-models)
- [Built-in repairs (structured-output, token-starvation)](#built-in-repairs-structured-output-token-starvation)
- [Connection pooling](#connection-pooling)
- [Wiring it into Hermes Agent](#wiring-it-into-hermes-agent)
- [Config reference](#config-reference)
- [Extending: custom repairs / discovery / providers](#extending-custom-repairs--discovery--providers)
- [Known limitations](#known-limitations)
- [Contributing / License](#contributing--license)

## Quickstart (60 seconds)

```bash
# 1 - clone and run the split proxies (compaction + toolchain, recommended)
git clone https://github.com/prashantjain25/hermes-race-proxy
cd hermes-race-proxy
python3 race_proxy_compaction.py --config race_proxy.example.json --verbose &
python3 race_proxy_toolchain.py --config race_proxy.example.json --port 8978 --verbose &

# 2 - in another terminal, confirm both are alive
curl -s http://127.0.0.1:8977/health   # compaction: long timeout
curl -s http://127.0.0.1:8978/health   # toolchain: short timeout

# 3 - send a real chat request, watch who wins the race
curl -s -X POST http://127.0.0.1:8977/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say hi in 3 words"}],"max_tokens":1500}'
```

Prefer one process instead? `race_proxy.py` still works exactly as before, one port, one shared timeout for everything pointed at it. See [Two proxies, one core: toolchain vs compaction](#two-proxies-one-core-toolchain-vs-compaction) for why the split exists and when it actually matters.

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

Point it at any combination of paid, free, local, or hosted backends. Swap the placeholders in `race_proxy.example.json` (or `.yaml`) for your own providers and models.

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
python3 race_proxy_compaction.py --config race_proxy.local.json          # port 8977, long timeout
python3 race_proxy_toolchain.py  --config race_proxy_toolchain.json      # port 8978, short timeout
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

`race_proxy.py` (the original single-process entrypoint) still works unmodified if you only run one auxiliary task through this proxy, or you'd rather manage one process than two. Nothing about the split changes what `race_proxy_core.py` does; it's the same racing/repair/contract logic served on two ports with two default timeouts instead of one.

## Project layout

| File | What it is |
|---|---|
| `race_proxy_compaction.py` | Recommended entrypoint for `auxiliary.compression`: long timeout, port 8977. |
| `race_proxy_toolchain.py` | Recommended entrypoint for `mcp`/`skills_hub`/`title_generation`: short timeout, port 8978. |
| `race_proxy.py` | Original single-process entrypoint. Still works, one port and one shared timeout for everything pointed at it. |
| `race_proxy_core.py` | Backend/race mechanics: assembling a request, racing backends via their configured `caller`, serving the endpoint. Knows nothing about *why* a response might be broken. |
| `repairs.py` | *Why* a response might be broken and how to fix it, behind a `RepairStrategy` interface. |
| `response_contracts.py` | *Was* a 200 response actually usable, per backend, behind a `ProviderContract` interface. See [Was the response actually usable?](#was-the-response-actually-usable-response-contracts) below. |
| `callers/` | *How* to reach a backend at all: pooled HTTP by default (`callers/http_caller.py`), or a CLI subprocess for a vendor with no HTTP API (`callers/cli_caller.py`). See [Reaching a backend](#reaching-a-backend-http-or-cli-callers) below. |
| `discovery.py` | Optional backend-selection extension point: probe/rank candidate models at proxy startup instead of hardcoding a static list. Off by default. |
| `connection_pool.py` | HTTP connection pooling (persistent per-backend connections) and the shared worker thread pool. |
| `providers/base.py`, `providers/pool.py` | Convenience layer for `discovery.py`, shared across every provider: the `Provider` base contract and the N-providers x M-models pool builder. |
| `providers/http/` | One file per HTTP-reachable vendor's connection contract (opencode.ai/zen, OpenRouter, DeepInfra, NVIDIA build.nvidia.com, Ollama). |
| `providers/cli/` | Empty by design. Holds a vendor's connection contract only once a real CLI-only integration (no HTTP API at all) is actually being added, see that directory's own docstring for why it stays empty otherwise. |
| `examples/custom_repairs_example.py` | Runnable template for a custom repair strategy. |
| `examples/custom_discovery_example.py` | Runnable template for a custom discovery policy, hand-rolled without `providers/`. |
| `examples/provider_pool_example.py` | The same policy rewritten on `providers/`, the recommended starting point once you want more than one provider. |
| `examples/cli_caller_example.py` | Runnable demo of the `Caller` interface (`callers/`) using `echo` as a stand-in CLI, no real vendor or key needed. |
| `examples/streaming_response_example.py` | Runnable, no-network demo of `wire_format.py`: exactly what bytes go out for a `stream: true` vs `stream: false` request. |

Default behavior with none of the extension points configured is an unchanged plain static `backends:` list in config; everything above is opt-in.

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
from providers.http.opencode_zen import OpenCodeZenProvider
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

Five providers ship today, all under `providers/http/`:

| Provider | `providers/http/*.py` | base_url | Needs a key for chat completions? |
|---|---|---|---|
| opencode.ai/zen | `opencode_zen.py` | `https://opencode.ai/zen/v1` | No, `-free` models are fully keyless |
| OpenRouter | `openrouter.py` | `https://openrouter.ai/api/v1` | Yes, confirmed 401 keyless, even on `:free`-suffixed models |
| DeepInfra | `deepinfra.py` | `https://api.deepinfra.com/v1/openai` | Yes, confirmed 401 keyless, no free-tier naming convention |
| NVIDIA build.nvidia.com | `nvidia_build.py` | `https://integrate.api.nvidia.com/v1` | Yes, credit-limited TRIAL service, not a stable free tier |
| Ollama (local) | `ollama.py` | `http://localhost:11434/v1` | No real auth, dummy key sent per Ollama's own client examples |

opencode.ai/zen, OpenRouter, DeepInfra, and NVIDIA build.nvidia.com are live-tested against the real vendor (curl, real key, real response, documented in each file). **Ollama is built from official docs only.** No local instance was available to test against; treat it as a starting point to verify.

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

1. **Structured-output rejection.** A `response_format: {"type": "json_schema", "strict": true, ...}` request 400s/422s at gateways that advertise Chat Completions compatibility without implementing strict-schema enforcement (e.g. opencode.ai/zen fronting `ling-3.0-flash-fin-free`, `nemotron-3.5-lightning-free`). The proxy retries with a fixed ladder: (1) original request, (2) drop `strict: true`, keep the schema, (3) drop `response_format` entirely (caller needs a loose-JSON-extraction fallback for this rung; Hermes's own `title_generator._extract_title_text` already has one).
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

Verified end-to-end: a strict-`json_schema` request with `max_tokens: 64` (Hermes's actual `title_generator.py` default) against `ling-3.0-flash-fin-free`, which 400s on the original request every time. Through the proxy: 8/8 trials returned a correct, schema-shaped title with `finish_reason: "stop"`, each via two stacked repairs (`response_format` dropped, then `max_tokens` boosted), tagged `"_race_proxy": {"repaired_rung": "format:2+tokens", ...}`.
</details>

## Connection pooling

Every backend gets a small pool of persistent HTTP connections (`connection_pool.py`) instead of a fresh TCP+TLS handshake per call, the same pattern a database client uses: pay setup cost once, reuse the connection, replace it transparently if the far end silently closed it while idle. The race thread pool is likewise created once at startup and reused for the process lifetime.

Pool stats are exposed on the health endpoint:

```bash
curl -s http://127.0.0.1:8977/health | python3 -m json.tool
# {
#   "status": "ok",
#   "backends": ["ling", "nemotron", "laguna"],
#   "timeout": 90,
#   "connection_pools": [
#     {"host": "opencode.ai", "port": 443, "in_use": 1, "idle": 2, "max_size": 8}
#   ]
# }
```

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
| `backends[].repair_token_starvation` | Default `true`. Retries once with `max_tokens` raised to `MIN_SAFE_MAX_TOKENS` (2000; edit the constant in `race_proxy.py` for a different floor). |
| `backends[].caller` | `"http"` (default) or `"cli"`. See [Reaching a backend](#reaching-a-backend-http-or-cli-callers) above. |
| `backends[].command` | Only used when `caller: "cli"`. Argv list for the vendor's official CLI. |

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
