# hermes-race-proxy

A tiny, zero-dependency local HTTP proxy that races a few OpenAI-compatible
LLM backends against each other and hands back whichever one answers first
with something usable. I built it for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
auxiliary-task routing, but it works with any OpenAI-compatible client.

## Project layout

The logic is split across a few small files, each independently
extensible without touching the others:

| File | What it is |
|---|---|
| `race_proxy.py` | Thin CLI entrypoint (arg parsing, logging setup). |
| `race_proxy_core.py` | HTTP mechanics: making a request, racing backends, serving the endpoint. Knows nothing about *why* a response might be broken. |
| `repairs.py` | *Why* a response might be broken and how to fix it, behind a `RepairStrategy` interface. Bring your own vendor-specific fix without editing anything else, see "Bring your own repair" below. |
| `discovery.py` | Optional backend-SELECTION extension point: probe/rank candidate models at proxy startup instead of hardcoding a static list. Off by default. |
| `connection_pool.py` | HTTP connection pooling (persistent per-backend connections, reused across requests) and the shared worker thread pool, the same "pay setup cost once, not per request" pattern a database client uses for connection pooling. |
| `providers/` | Optional convenience layer for `discovery.py`: one file per LLM vendor's connection contract (base_url, auth style, model listing), cloud or local, anything speaking the OpenAI chat-completions shape, plus `providers/pool.py` to build an N providers × M models-per-provider flat backend list. See "Bring your own backend-discovery policy" below. |
| `examples/custom_repairs_example.py` | Runnable template for plugging in your own repair strategy. |
| `examples/custom_discovery_example.py` | Runnable template for plugging in your own backend-discovery policy, hand-rolled without `providers/`. |
| `examples/provider_pool_example.py` | The same policy as above, rewritten on top of `providers/`, the recommended starting point once you want more than one provider. |

Default behavior with none of the extension points configured is
unchanged from a plain static `backends:` list in config, everything
above is opt-in.

## Why this exists

I was digging into how Hermes routes its auxiliary tasks (skill selection,
MCP tool routing, approval checks, that kind of thing) and noticed
`auxiliary.<task>.fallback_chain` only tries backends one at a time: hit the
primary, and only if that fails or times out does it move to the next one
in the list. That's fine for most tasks. It's annoying for the ones that
fire on every single turn, where you end up eating the primary's full
timeout before the fallback even gets a shot.

So instead of waiting on one, then the other, this proxy just fires the
request at everything you've configured at the same time and takes whoever
answers first. Worst case you're bound by your slowest backend's timeout
instead of the sum of all of them.

## How the race actually works

1. A request comes in to `/v1/chat/completions`.
2. The proxy sends that same request to every backend you've configured, all
   at once, each on its own thread.
3. As answers come back, the proxy checks whether they're actually usable.
   A response only counts if it has real content and, by default,
   `finish_reason: "stop"`. This matters more than it sounds like it should:
   some free-tier reasoning models will happily burn their whole
   `max_tokens` budget on invisible reasoning and hand you back an empty
   string. The proxy treats that as a non-answer and keeps waiting on
   whoever else is still in flight.
4. First real answer wins. It gets sent back to the client tagged with
   `_race_proxy.winner` and `_race_proxy.latency`, so you can see who won
   and by how much.
5. Everyone else still running just finishes on their own thread and gets
   thrown away. Nothing is delayed waiting for the losers.

I tested this against a keyless free-tier endpoint running two different
models, but there's nothing model-specific baked in. Point it at whatever
combination of paid, free, local, or hosted backends you want to race.
Swap the placeholders in `race_proxy.example.json` (or `.yaml`) for your
own providers and models.

## Structured-output and token-starvation repair (built-in strategies)

This came out of a real, reproducible failure: I was routing an
OpenAI-compatible request with `response_format: {"type": "json_schema",
"json_schema": {"strict": true, ...}}` at a free-tier gateway
(opencode.ai/zen, fronting `ling-3.0-flash-fin-free` and
`nemotron-3.5-lightning-free`), and every single call came back as an
opaque `400`:

```
Error code: 400 - {"error": {"type": "server_error", "message":
"Error from provider (Console): Upstream request failed: [400] Provider
returned error"}}
```

The message says nothing about `response_format`, `json_schema`, or
`strict`, it's a generic wrapper. That matters because Hermes Agent's own
built-in retry (`_is_structured_output_rejection` in
`agent/auxiliary_client.py`) only fires when the error text contains one
of a handful of known substrings. This vendor's error contains none of
them, so the retry path never triggers and the call just fails, every
time, permanently.

I don't think that's really an opencode.ai-specific bug, or a Hermes bug,
it's a structural mismatch. `strict: true` JSON-Schema structured output
is an OpenAI-specific contract, and plenty of OpenAI-*compatible* gateways
advertise Chat Completions compatibility without actually implementing
the strict-schema enforcement machinery behind it. NVIDIA's own Nemotron
docs recommend loose `json_object` mode instead of strict `json_schema`
for exactly this reason. A separate `airframe` adapter write-up notes
`STRUCTURED_OUTPUT_STRICT stays False, compat-vendor coverage is uneven`.
And an OpenRouter `ai-sdk-provider` issue (#483) describes the identical
failure shape for a different vendor entirely: hardcoded `strict: true`
excludes every endpoint that doesn't support it, and the resulting error
message doesn't say why.

You can chase every vendor's specific error string and add it to a
detector list, some clients do this, Hermes does this, but you're
always one new vendor behind. So instead of trying to *detect* the cause
of a 400, the proxy just tries a fixed ladder of looser request shapes,
in order, whenever a request that carried a `response_format` comes back
400 or 422:

1. **Original request**, unmodified. If it works, nothing else happens.
2. **Drop `strict: true`** on the `json_schema`, keep the schema. Some
   backends support `json_schema` mode but reject strict enforcement
   specifically.
3. **Drop `response_format` entirely.** Whatever schema was requested now
   degrades to plain-text compliance with your system/user prompt. The
   caller needs a loose-JSON-extraction fallback for this to still work,
   Hermes's own `title_generator._extract_title_text` already has one, and
   most JSON-mode prompting patterns do too.

Separately, and this bit further down the same failure chain, free-tier
reasoning models can come back `200 OK` with **empty content** and
`finish_reason: "length"`, because they spent the entire `max_tokens`
budget on hidden reasoning before writing anything visible. A caller that
sizes `max_tokens` for a short answer (a session title is a handful of
words) can starve the model completely without ever seeing an error. The
proxy detects this shape specifically, empty content **and**
`finish_reason: "length"`, not just "the answer was short", and retries
once with `max_tokens` bumped to a safe floor (2000 by default,
`MIN_SAFE_MAX_TOKENS` in the source).

Both repairs are entirely request/response-shape driven, neither one
cares which backend or model produced the 400 or the starved 200. Point
the proxy at any OpenAI-compatible vendor, current or future, and the
same ladder applies without touching a line of code. That's also why this
lives in the proxy rather than in Hermes itself: Hermes's own detector
has to be re-taught every time a new vendor phrases its rejection
differently, and that fix lives inside `hermes-agent`'s own git tree,
where `hermes update` (a `git pull`) can overwrite or conflict with a
local patch. This proxy sits outside that tree entirely, nothing about
running it depends on which Hermes version, or even which LLM client, is
calling it.

Verified end-to-end against the real failure: a strict-`json_schema`
request with `max_tokens: 64` (Hermes's actual `title_generator.py`
default) against `ling-3.0-flash-fin-free`, which 400s on the original
request every time. Through the proxy: 8/8 trials returned a correct,
schema-shaped title with `finish_reason: "stop"`, each one via two
automatic repairs stacked (`response_format` dropped, then `max_tokens`
boosted), tagged in the response as `"_race_proxy": {"repaired_rung":
"format:2+tokens", ...}`.

Both repairs are on by default per backend. Turn either off per-backend
in your config if you'd rather see the raw failure:

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

## Bring your own repair strategy

The two built-in repairs above live in `repairs.py` behind a small
interface (`RepairStrategy`), not hardcoded into the request-handling
code. If you hit a DIFFERENT vendor's failure shape, some other
rejected field, some other truncation pattern, you don't need to fork
this repo or send a PR to get it fixed for your setup:

1. Write a standalone `.py` file anywhere on disk. Subclass
   `RepairStrategy` from `repairs.py`, implement `applies()` (does this
   result match my failure shape?) and `propose()` (what request body
   should I retry with?).
2. Define a module-level `register(registry) -> None` that calls
   `registry.register(YourStrategy())`.
3. Point your proxy config at it: `"custom_repairs_module":
   "/path/to/my_repairs.py"`.

See `examples/custom_repairs_example.py` for a complete, runnable
template (a real strategy: dropping unknown top-level fields some
vendors reject with a strict-validator 400).

## Bring your own backend-discovery policy

The static `backends:` list in config is still the default and always
works. But which models to race, how many, from which providers, and
how to rank them at startup is a personal policy call, not something
that belongs hardcoded into a project other people install. `discovery.py`
is the extension point for that:

1. Write a standalone `.py` file. Define a module-level
   `discover_backends(cfg: dict) -> list[Backend]` that returns however
   many `Backend` instances you want racing, probe a provider's model
   catalog, rank by measured latency, mix providers, whatever your
   policy is.
2. Point your config at it: `"custom_discovery_module":
   "/path/to/my_discovery.py"`.
3. This runs ONCE at proxy startup, not per-request, an exhaustive
   probe of a dozen candidate models to pick the fastest few is a
   reasonable thing to do here, since it never adds latency to a real
   chat-completion call later.
4. If your module fails to load, raises, or returns nothing, the proxy
   falls back to the static `backends:` list in config and logs a
   warning, a broken discovery script degrades to "no discovery,"
   never to "no backends at all."

See `examples/custom_discovery_example.py` for a complete, runnable
policy: 2 fixed backends always included, plus the top 2 fastest models
from an exhaustive startup probe of another provider's full catalog
(candidates run in parallel, ranked by measured response latency, only
successful responders kept).

## Provider layer (`providers/`): N providers × M models

`discovery.py`'s custom policy above still leaves you re-deriving each
vendor's connection contract (base_url, auth style, model-listing
shape) inline. `providers/` factors that out into one small object per
vendor, and `providers/pool.py` turns a list of "N enabled providers,
each contributing M models" into one flat backend list, the total is

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

See `examples/provider_pool_example.py` for this exact policy as a
complete, runnable `custom_discovery_module`, verified end-to-end
against live opencode.ai/zen and build.nvidia.com endpoints.

Four cloud providers ship with real, verified connection contracts (base_url
+ auth requirement, checked live against each vendor as of Aug 2026), plus
one local one built from official docs (see "Add your own provider" further
down for the honesty caveat on that one):

| Provider | `providers/*.py` | base_url | Needs a key for chat completions? |
|---|---|---|---|
| opencode.ai/zen | `opencode_zen.py` | `https://opencode.ai/zen/v1` | No, `-free` models are fully keyless |
| OpenRouter | `openrouter.py` | `https://openrouter.ai/api/v1` | Yes, confirmed 401 keyless, even on `:free`-suffixed models |
| DeepInfra | `deepinfra.py` | `https://api.deepinfra.com/v1/openai` | Yes, confirmed 401 keyless, no free-tier model naming convention |
| NVIDIA build.nvidia.com | `nvidia_build.py` | `https://integrate.api.nvidia.com/v1` | Yes, credit-limited TRIAL service, not a stable free tier (see caveat below) |
| Ollama (local) | `ollama.py` | `http://localhost:11434/v1` | No real auth, dummy key sent per Ollama's own client examples |

Adding your own provider (self-hosted vLLM, Together, Groq, whatever) is
one new file subclassing `Provider` from `providers/base.py`, override
`base_url`, `requires_api_key`, and `default_headers()` if the vendor
needs anything beyond a bearer token. `list_models()` works unmodified
for any vendor whose `/v1/models` listing matches the standard
`{"data": [{"id": "..."}]}` shape; override it if not.

## Connection pooling

Every backend gets a small pool of persistent HTTP connections
(`connection_pool.py`) instead of opening a fresh TCP+TLS handshake on
every single chat-completion call, the same pattern a database client
uses for connection pooling: pay setup cost once, reuse the connection
across requests, replace one transparently if the far end silently
closed it while idle. The thread pool that races backends in parallel is
also created once at proxy startup and reused for the life of the
process, not spun up fresh inside every request.

Pool stats are exposed on the health endpoint for observability, the
same shape a database pool's metrics endpoint typically shows:

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

## What I cared about while building this

- **Reasoning models shouldn't be able to "win" with garbage.** See the
  empty-content problem above. `require_finish_reason: stop` (on by
  default) catches most of it.
- **No dependencies you didn't already have.** Pure Python 3 standard
  library: `http.server`, `concurrent.futures`, `urllib`. YAML config needs
  `pyyaml` if you want it, but JSON works out of the box with nothing extra
  to install.
- **It shouldn't add a new way to leak your credentials.** The proxy
  doesn't generate or store anything; it just forwards whatever
  `api_key`/headers you put in the config, same as any HTTP client would.
  It binds to `127.0.0.1` by default and has zero auth of its own, so don't
  put it on a public interface without adding some yourself.

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

## Wiring it into Hermes Agent

Point an auxiliary task's `base_url` at the proxy instead of the real
provider:

```yaml
auxiliary:
  skills_hub:
    provider: custom
    base_url: http://127.0.0.1:8977/v1
    model: race-proxy   # ignored, the proxy swaps in each backend's real
                          # model name from its own config
```

Start the proxy before you start Hermes, and let your process supervisor of
choice (systemd, launchd, pm2, whatever you already use) keep it running.

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
| `timeout` | How long a single race is allowed to run before giving up and returning a `502`. Each backend's internal repair retries share this same budget, see "Structured-output and token-starvation repair" above. If you enable both repairs, a backend can make up to 4 HTTP attempts against one incoming request, so don't set `timeout` too low or you'll cut off a repair mid-retry. 60-90s is a reasonable range. |
| `require_finish_reason` | Set to `null` or leave it out to accept any completion, truncated or not. Default `"stop"` rejects `finish_reason: "length"` results, which is the usual failure mode when a reasoning model gets too small a `max_tokens`. |
| `backends[].api_key` | An empty string sends an explicit empty `Authorization` header. Some keyless free tiers 401 you the moment they see any recognized bearer token format, even a made-up one, so this matters. |
| `backends[].repair_structured_output` | Default `true`. On a 400/422 for a request that carried `response_format`, retries with `strict` dropped, then with `response_format` removed entirely. See the section above. |
| `backends[].repair_token_starvation` | Default `true`. On a 200 with empty content and `finish_reason: "length"`, retries once with `max_tokens` raised to `MIN_SAFE_MAX_TOKENS` (2000, edit the constant in `race_proxy.py` if you need a different floor). |

## Things I haven't solved yet

- **You're now sending 2x, 3x, however many requests you're racing.**
  If two of your backends share a rate limit, racing them can burn through
  that limit faster, not slower. Worth watching before you assume this is
  a clean win for your setup. The structured-output/token-starvation
  repairs multiply this further per-backend (up to 4 attempts per request
  on a backend that needs both repairs), still free on keyless tiers, but
  worth knowing if you're paying per token.
- **No caching.** Every request races from a cold start.
- **No streaming.** Responses get buffered fully before returning. This is
  probably the biggest gap right now, and I'd genuinely welcome a PR for it.
- **No auth on the proxy itself.** It's meant to live on localhost. Put a
  real reverse proxy with real auth in front if you need to expose it.
- **The token-starvation floor is a single constant, not per-model.**
  `MIN_SAFE_MAX_TOKENS` (2000) is a global default across all backends.
  A model with unusually heavy reasoning overhead might still starve at
  2000, raise the constant if you hit that.
- **Custom discovery scripts that call trial/credit-limited APIs need
  their own judgment about production suitability.** The example
  discovery script (`examples/custom_discovery_example.py`) probes
  NVIDIA's build.nvidia.com hosted catalog, which is explicitly a TRIAL
  service under NVIDIA's own API Trial Terms of Service, credit-limited,
  rate-limited around ~40 RPM account-wide and undocumented, and NOT
  licensed for production traffic per NVIDIA's own FAQ. Treat any
  discovered backend from a trial/free tier as a best-effort
  supplementary racer, not a guaranteed-available backend, race it
  alongside more predictable fixed backends, not alone.
- I tested this by hand against a live free-tier endpoint. There's no
  automated test suite yet, so treat it accordingly until one exists.

## Why bother with any of this

A few concrete reasons this exists as its own thing instead of a config tweak.

**Free models stop working the moment everyone finds out about them.** opencode.ai/zen's `-free` tier gets hammered by every agent framework that discovers it, and the failure mode isn't a clean 429 with a `Retry-After` header, it's an opaque 400 wrapped three layers deep, or a silent empty response, or an endpoint that flaps between 200 and 503 minute to minute. Racing several free models (or free plus local plus a cheap paid fallback) means one model's bad day doesn't take your whole pipeline down with it, and it spreads your actual request volume across providers instead of hammering one shared pool the way every other unmodified client on that provider already is.

**Most auxiliary calls don't need your best model, they need an answer.** Title generation, skill routing, MCP tool selection, approval checks: these fire on nearly every turn and the task is genuinely small (pick a title, pick a tool, yes or no). Racing free/cheap/local models for this class of call means your paid model's budget goes toward the turns that actually need it, not toward naming a chat session "Fix nginx 502 error." I walked through the whole toolchain doing this for Hermes: `title_generation`, `skills_hub`, `mcp`, `approval`, `triage_specifier`, `kanban_decomposer`, `profile_describer`, `curator`, `monitor`, `memory_query_rewrite`, `goal_judge` all route through opencode.ai/zen's free tier today, with `compression` on a dedicated paid model because that one call is quality-sensitive and can't afford a bad answer. Every one of those calls that lands on a free or local model is one less call against your Anthropic/OpenAI/OpenRouter usage.

**One provider seeing your entire request history is a bigger blast radius than a few providers each seeing a slice of it.** Every request that goes through a single vendor gives that vendor the full pattern: what you're asking, how often, what your session titles reveal about your work. Splitting auxiliary traffic across several providers, including a local model that never leaves your machine, means no single party holds the complete picture. This isn't a defense against a targeted attacker, it's just less surface area for casual correlation, and a local Ollama/vLLM/llama.cpp backend in the race means at least some fraction of your traffic never crosses the network at all.

**Getting an auxiliary call wrong costs more than the call itself.** A title_generation 400 that Hermes retries three times before giving up burns real conversation-turn budget on a task that was supposed to be free overhead. A skill lookup that times out delays the actual user-facing response behind it. None of this shows up as a token bill, it shows up as your main conversation eating latency and retry cycles it shouldn't have to. Getting the auxiliary layer to just work quietly in the background is worth protecting on its own.

## Why this lives outside Hermes, not inside it

I looked at whether this belongs as a PR into `hermes-agent` itself and decided against it, for reasons that are specific to how that project is built, not general skepticism about upstreaming.

Hermes's own contribution guidelines are explicit about this tradeoff: keep the core narrow, push capability to the edges. New model tools get called "the expensive exception" in that doc, because every tool ships on every API call, and the documented order of preference is extend existing code, then a CLI command plus a skill, then a service-gated tool, then a plugin, then an MCP server in the catalog, and only as a last resort a new core tool. A parallel-racing HTTP proxy with a pluggable repair/discovery/provider system is squarely a plugin-shaped problem, not a core-tool-shaped one. It doesn't need a new tool schema on every model call, it needs to sit behind `provider: custom, base_url: http://127.0.0.1:PORT/v1`, a config shape Hermes already supports for exactly this kind of thing.

The other reason is durability. `~/.hermes/hermes-agent` is a live git checkout that `hermes update` pulls against directly. Anything merged into core, or patched locally into that tree, gets overwritten or conflicts on the next update. A standalone process that Hermes talks to over `provider: custom` never touches that tree at all, so it survives every update by construction, not by discipline.

The last reason is that this genuinely isn't Hermes-specific. Any OpenAI-compatible client, `curl`, LangChain, a homegrown script, gets the exact same benefit from pointing at this proxy instead of a single backend. Baking it into Hermes's core would make it a Hermes feature; leaving it as a standalone proxy makes it a tool that happens to plug into Hermes cleanly.

None of this rules out a lighter-weight path back into the ecosystem later: a `hermes-race-proxy` skill that documents the setup for Hermes users specifically, or a small MCP server wrapper, both fit the "extend at the edges" model Hermes's own contributor guide asks for. What doesn't fit is folding the racing/repair/discovery logic itself into `agent/auxiliary_client.py`.

## Add your own provider (cloud or local)

Five providers ship with this repo: opencode.ai/zen, OpenRouter, DeepInfra, and NVIDIA build.nvidia.com are all live-tested against the real vendor (curl, real key, real response, documented in each file). Ollama is built from its official docs only, I don't have a local instance running to test against, so treat that one file as a starting point someone with an actual Ollama box should verify and fix if anything's off. If you run vLLM, LM Studio, llama.cpp's server mode, Groq, Together, Fireworks, or anything else that speaks the OpenAI chat-completions shape, adding it is one file:

```python
from providers.base import Provider

class MyProvider(Provider):
    name = "my-provider"
    base_url = "https://api.my-provider.example.com/v1"
    requires_api_key = True  # or False for a keyless/local endpoint

    def default_headers(self) -> dict:
        return {}  # anything beyond Authorization this vendor wants
```

`list_models()` works unmodified if the vendor's `/v1/models` returns the standard `{"data": [{"id": "..."}]}` shape (most do); override it if not. Drop the file in `providers/`, add a `ProviderSlot` for it in your discovery script, and it races alongside whatever else you've configured, cloud and local together, with the same structured-output and token-starvation repairs applying automatically since those live in `repairs.py`, not per-provider.

If you build one worth sharing, a PR against this repo adding your provider file (with the same "verified live, here's how" discipline the four cloud providers follow) is exactly the kind of contribution this project wants.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
