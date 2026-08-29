# hermes-race-proxy

A tiny, zero-dependency local HTTP proxy that races a few OpenAI-compatible
LLM backends against each other and hands back whichever one answers first
with something usable. Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
auxiliary-task routing, but works with any OpenAI-compatible client.

**Contents**
- [Quickstart](#quickstart)
- [How the race works](#how-the-race-works)
- [Why this exists](#why-this-exists)
- [Project layout](#project-layout)
- [Providers and pooling (N providers x M models)](#providers-and-pooling-n-providers-x-m-models)
- [Built-in repairs (structured-output, token-starvation)](#built-in-repairs-structured-output-token-starvation)
- [Connection pooling](#connection-pooling)
- [Wiring it into Hermes Agent](#wiring-it-into-hermes-agent)
- [Config reference](#config-reference)
- [Extending: custom repairs / discovery / providers](#extending-custom-repairs--discovery--providers)
- [Known limitations](#known-limitations)
- [Contributing / License](#contributing--license)

## Quickstart

```bash
git clone https://github.com/<you>/hermes-race-proxy
cd hermes-race-proxy
python3 race_proxy.py --config race_proxy.example.json --verbose
```

In another terminal:

```bash
curl -s http://127.0.0.1:8977/health

curl -s -X POST http://127.0.0.1:8977/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say hi in 3 words"}],"max_tokens":1500}'
```

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
3. A response only counts if it has real content and, by default, `finish_reason: "stop"`. Some free-tier reasoning models burn their whole `max_tokens` budget on invisible reasoning and hand back an empty string — the proxy treats that as a non-answer and keeps waiting on whoever else is still in flight.
4. First real answer wins, tagged with `_race_proxy.winner` and `_race_proxy.latency`.
5. Everyone else still running finishes on their own thread and gets thrown away — nothing waits on the losers.

Point it at any combination of paid, free, local, or hosted backends. Swap the placeholders in `race_proxy.example.json` (or `.yaml`) for your own providers and models.

## Why this exists

Hermes routes its auxiliary tasks (skill selection, MCP tool routing, approval checks, title generation, etc.) through `auxiliary.<task>.fallback_chain`, which tries backends **one at a time**: hit the primary, and only on failure/timeout does it move to the next. Fine for rare calls, annoying for the ones that fire on every turn — you eat the primary's full timeout before the fallback gets a shot. This proxy fires the request at everything configured, simultaneously, and takes whoever answers first. Worst case you're bound by your slowest backend's timeout, not the sum of all of them.

<details>
<summary>Four concrete reasons this is a standalone proxy, not a Hermes core feature or PR</summary>

**Free models stop working the moment everyone finds out about them.** opencode.ai/zen's `-free` tier gets hammered by every agent framework that discovers it — failures show up as an opaque 400 wrapped three layers deep, a silent empty response, or an endpoint flapping between 200 and 503 minute to minute. Racing several free/local/cheap-paid models means one model's bad day doesn't take the whole pipeline down, and spreads load instead of hammering one shared pool.

**Most auxiliary calls don't need your best model, they need an answer.** Title generation, skill routing, tool selection, approval checks — small, frequent, low-stakes. Routing these through free/cheap/local models keeps your paid model's budget for turns that actually need it. In practice: `title_generation`, `skills_hub`, `mcp`, `approval`, `triage_specifier`, `kanban_decomposer`, `profile_describer`, `curator`, `monitor`, `memory_query_rewrite`, `goal_judge` all route through opencode.ai/zen's free tier; `compression` stays on a dedicated paid model since that call is quality-sensitive.

**One provider seeing your entire request history is a bigger blast radius than several providers each seeing a slice.** Splitting auxiliary traffic across providers (including a local model that never leaves the machine) means no single party holds the complete picture. Not a defense against a targeted attacker — just less surface area for casual correlation.

**Getting an auxiliary call wrong costs more than the call itself.** A failed title_generation call that Hermes retries three times burns conversation-turn budget on what was supposed to be free overhead. Keeping the auxiliary layer working quietly is worth protecting on its own.

**Why not a PR into hermes-agent core instead:** Hermes's own contribution guidelines push capability to the edges and call new model tools "the expensive exception" (every tool ships on every API call). A parallel-racing proxy with pluggable repair/discovery/provider logic is plugin-shaped, not core-tool-shaped — it just needs `provider: custom, base_url: http://127.0.0.1:PORT/v1`, a shape Hermes already supports. It's also more durable: `~/.hermes/hermes-agent` is a live git checkout that `hermes update` pulls against directly, so anything merged/patched into that tree risks being overwritten. A standalone process talked to over `provider: custom` never touches that tree, so it survives every update by construction. And it isn't Hermes-specific — any OpenAI-compatible client (curl, LangChain, a homegrown script) benefits the same way.

A lighter-weight path back into the ecosystem later (a documentation skill, or a thin MCP server wrapper) both fit Hermes's "extend at the edges" model. Folding the racing/repair/discovery logic itself into `agent/auxiliary_client.py` does not.
</details>

## Project layout

| File | What it is |
|---|---|
| `race_proxy.py` | Thin CLI entrypoint (arg parsing, logging setup). |
| `race_proxy_core.py` | HTTP mechanics: making a request, racing backends, serving the endpoint. Knows nothing about *why* a response might be broken. |
| `repairs.py` | *Why* a response might be broken and how to fix it, behind a `RepairStrategy` interface. |
| `discovery.py` | Optional backend-selection extension point: probe/rank candidate models at proxy startup instead of hardcoding a static list. Off by default. |
| `connection_pool.py` | HTTP connection pooling (persistent per-backend connections) and the shared worker thread pool. |
| `providers/` | Convenience layer for `discovery.py`: one file per LLM vendor's connection contract, plus `providers/pool.py` to build an N-providers x M-models flat backend list. |
| `examples/custom_repairs_example.py` | Runnable template for a custom repair strategy. |
| `examples/custom_discovery_example.py` | Runnable template for a custom discovery policy, hand-rolled without `providers/`. |
| `examples/provider_pool_example.py` | The same policy rewritten on `providers/` — the recommended starting point once you want more than one provider. |

Default behavior with none of the extension points configured is an unchanged plain static `backends:` list in config — everything above is opt-in.

## Providers and pooling (N providers x M models)

`providers/` factors each vendor's connection contract (base_url, auth style, model-listing shape) into one small object, and `providers/pool.py` turns "N enabled providers, each contributing M models" into one flat backend list:

    total backends = sum(top_n for each enabled ProviderSlot)

```python
from providers.opencode_zen import OpenCodeZenProvider
from providers.nvidia_build import NvidiaBuildProvider
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

Five providers ship today:

| Provider | `providers/*.py` | base_url | Needs a key for chat completions? |
|---|---|---|---|
| opencode.ai/zen | `opencode_zen.py` | `https://opencode.ai/zen/v1` | No, `-free` models are fully keyless |
| OpenRouter | `openrouter.py` | `https://openrouter.ai/api/v1` | Yes, confirmed 401 keyless, even on `:free`-suffixed models |
| DeepInfra | `deepinfra.py` | `https://api.deepinfra.com/v1/openai` | Yes, confirmed 401 keyless, no free-tier naming convention |
| NVIDIA build.nvidia.com | `nvidia_build.py` | `https://integrate.api.nvidia.com/v1` | Yes, credit-limited TRIAL service, not a stable free tier |
| Ollama (local) | `ollama.py` | `http://localhost:11434/v1` | No real auth, dummy key sent per Ollama's own client examples |

opencode.ai/zen, OpenRouter, DeepInfra, and NVIDIA build.nvidia.com are live-tested against the real vendor (curl, real key, real response, documented in each file). **Ollama is built from official docs only** — no local instance was available to test against; treat it as a starting point to verify.

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

`list_models()` works unmodified if the vendor's `/v1/models` returns the standard `{"data": [{"id": "..."}]}` shape (most do); override it if not. Drop the file in `providers/`, add a `ProviderSlot` for it in your discovery script, and it races alongside everything else configured — cloud and local together — with the same structured-output and token-starvation repairs applying automatically (they live in `repairs.py`, not per-provider).

PRs adding a new provider file are welcome, with the same "verified live, here's how" discipline the four cloud providers follow.
</details>

## Built-in repairs (structured-output, token-starvation)

Two failure modes hit real usage and get auto-repaired without needing to detect the vendor's specific error string:

1. **Structured-output rejection.** A `response_format: {"type": "json_schema", "strict": true, ...}` request 400s/422s at gateways that advertise Chat Completions compatibility without implementing strict-schema enforcement (e.g. opencode.ai/zen fronting `ling-3.0-flash-fin-free`, `nemotron-3.5-lightning-free`). The proxy retries with a fixed ladder: (1) original request, (2) drop `strict: true` keep the schema, (3) drop `response_format` entirely (caller needs a loose-JSON-extraction fallback for this rung — Hermes's own `title_generator._extract_title_text` already has one).
2. **Token starvation.** Free-tier reasoning models can return `200 OK` with empty content and `finish_reason: "length"` because the entire `max_tokens` budget went to hidden reasoning before any visible output. The proxy detects that exact shape (empty content **and** `finish_reason: "length"`) and retries once with `max_tokens` raised to a safe floor (`MIN_SAFE_MAX_TOKENS`, 2000 by default).

Both repairs are request/response-shape driven — they don't care which vendor produced the failure, so the same ladder applies to any OpenAI-compatible backend without code changes. That portability is also why this lives in the proxy instead of Hermes: Hermes's own `_is_structured_output_rejection` detector in `agent/auxiliary_client.py` only fires on known error-text substrings, so it's always one new vendor behind, and that fix lives inside `hermes-agent`'s own git tree where `hermes update` can overwrite a local patch.

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

No mention of `response_format`, `json_schema`, or `strict` — a generic wrapper. NVIDIA's own Nemotron docs recommend loose `json_object` mode instead of strict `json_schema` for the same reason; an OpenRouter `ai-sdk-provider` issue (#483) documents the identical failure shape for a different vendor entirely.

Verified end-to-end: a strict-`json_schema` request with `max_tokens: 64` (Hermes's actual `title_generator.py` default) against `ling-3.0-flash-fin-free`, which 400s on the original request every time. Through the proxy: 8/8 trials returned a correct, schema-shaped title with `finish_reason: "stop"`, each via two stacked repairs (`response_format` dropped, then `max_tokens` boosted), tagged `"_race_proxy": {"repaired_rung": "format:2+tokens", ...}`.
</details>

## Connection pooling

Every backend gets a small pool of persistent HTTP connections (`connection_pool.py`) instead of a fresh TCP+TLS handshake per call — the same pattern a database client uses: pay setup cost once, reuse the connection, replace it transparently if the far end silently closed it while idle. The race thread pool is likewise created once at startup and reused for the process lifetime.

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
| `timeout` | How long a single race runs before giving up and returning `502`. Each backend's internal repair retries share this budget — with both repairs enabled a backend can make up to 4 HTTP attempts per incoming request, so don't set this too low. 60-90s is reasonable. |
| `require_finish_reason` | Set `null`/omit to accept truncated completions. Default `"stop"` rejects `finish_reason: "length"` results (the usual failure mode when a reasoning model gets too small a `max_tokens`). |
| `backends[].api_key` | An empty string sends an explicit empty `Authorization` header — some keyless free tiers 401 you the moment they see any recognized bearer-token format. |
| `backends[].repair_structured_output` | Default `true`. See "Built-in repairs" above. |
| `backends[].repair_token_starvation` | Default `true`. Retries once with `max_tokens` raised to `MIN_SAFE_MAX_TOKENS` (2000; edit the constant in `race_proxy.py` for a different floor). |

## Extending: custom repairs / discovery / providers

**Custom repair strategy** — hit a different vendor's failure shape (rejected field, truncation pattern)? No fork/PR needed:
1. Write a standalone `.py` file. Subclass `RepairStrategy` from `repairs.py`, implement `applies()` and `propose()`.
2. Define `register(registry) -> None` calling `registry.register(YourStrategy())`.
3. Point config at it: `"custom_repairs_module": "/path/to/my_repairs.py"`.

See `examples/custom_repairs_example.py` (dropping unknown top-level fields some vendors reject with a strict-validator 400).

**Custom discovery policy** — which models to race, how many, from which providers, and how to rank them at startup is a personal policy call:
1. Write a standalone `.py` file defining `discover_backends(cfg: dict) -> list[Backend]`.
2. Point config at it: `"custom_discovery_module": "/path/to/my_discovery.py"`.
3. Runs once at proxy startup, not per-request — an exhaustive probe of a dozen candidate models to pick the fastest few is reasonable here since it never adds latency to a real chat-completion call later.
4. A module that fails to load, raises, or returns nothing degrades to the static `backends:` list with a logged warning — never to "no backends at all."

See `examples/custom_discovery_example.py` (2 fixed backends plus the top 2 fastest of an exhaustive startup probe, candidates run in parallel).

**Custom provider** — see "Add your own provider" under [Providers and pooling](#providers-and-pooling-n-providers-x-m-models) above.

## Known limitations

- **You're now sending 2x, 3x, however many requests you're racing.** If backends share a rate limit, racing burns through it faster. The structured-output/token-starvation repairs multiply this further (up to 4 attempts per request on a backend needing both), free on keyless tiers but worth knowing if paying per token.
- **No caching.** Every request races from a cold start.
- **No streaming.** Responses are buffered fully before returning — the biggest gap right now; a PR here is welcome.
- **No auth on the proxy itself.** Meant to live on localhost. Put a real reverse proxy with real auth in front if exposing it.
- **The token-starvation floor is a single global constant, not per-model.** `MIN_SAFE_MAX_TOKENS` (2000) applies across all backends; raise it if a heavy-reasoning-overhead model still starves.
- **Trial/credit-limited discovery APIs need your own production judgment.** The example discovery script probes NVIDIA's build.nvidia.com catalog, an explicit TRIAL service under NVIDIA's API Trial Terms (credit-limited, ~40 RPM account-wide, undocumented, not licensed for production traffic). Treat any trial/free-tier discovered backend as best-effort supplementary, racing alongside more predictable fixed backends — not alone.
- **No automated test suite yet.** Tested by hand against live free-tier endpoints; treat accordingly.

## Contributing / License

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed.
