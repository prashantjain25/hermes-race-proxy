# hermes-race-proxy

A tiny, zero-dependency local HTTP proxy that races a few OpenAI-compatible
LLM backends against each other and hands back whichever one answers first
with something usable. I built it for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
auxiliary-task routing, but it works with any OpenAI-compatible client.

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

## Structured-output and token-starvation repair

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
`strict` — it's a generic wrapper. That matters because Hermes Agent's own
built-in retry (`_is_structured_output_rejection` in
`agent/auxiliary_client.py`) only fires when the error text contains one
of a handful of known substrings. This vendor's error contains none of
them, so the retry path never triggers and the call just fails, every
time, permanently.

I don't think that's really an opencode.ai-specific bug, or a Hermes bug —
it's a structural mismatch. `strict: true` JSON-Schema structured output
is an OpenAI-specific contract, and plenty of OpenAI-*compatible* gateways
advertise Chat Completions compatibility without actually implementing
the strict-schema enforcement machinery behind it. NVIDIA's own Nemotron
docs recommend loose `json_object` mode instead of strict `json_schema`
for exactly this reason. A separate `airframe` adapter write-up notes
`STRUCTURED_OUTPUT_STRICT stays False — compat-vendor coverage is uneven`.
And an OpenRouter `ai-sdk-provider` issue (#483) describes the identical
failure shape for a different vendor entirely: hardcoded `strict: true`
excludes every endpoint that doesn't support it, and the resulting error
message doesn't say why.

You can chase every vendor's specific error string and add it to a
detector list — some clients do this, Hermes does this — but you're
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
   caller needs a loose-JSON-extraction fallback for this to still work —
   Hermes's own `title_generator._extract_title_text` already has one, and
   most JSON-mode prompting patterns do too.

Separately — and this bit further down the same failure chain — free-tier
reasoning models can come back `200 OK` with **empty content** and
`finish_reason: "length"`, because they spent the entire `max_tokens`
budget on hidden reasoning before writing anything visible. A caller that
sizes `max_tokens` for a short answer (a session title is a handful of
words) can starve the model completely without ever seeing an error. The
proxy detects this shape specifically — empty content **and**
`finish_reason: "length"`, not just "the answer was short" — and retries
once with `max_tokens` bumped to a safe floor (2000 by default,
`MIN_SAFE_MAX_TOKENS` in the source).

Both repairs are entirely request/response-shape driven — neither one
cares which backend or model produced the 400 or the starved 200. Point
the proxy at any OpenAI-compatible vendor, current or future, and the
same ladder applies without touching a line of code. That's also why this
lives in the proxy rather than in Hermes itself: Hermes's own detector
has to be re-taught every time a new vendor phrases its rejection
differently, and that fix lives inside `hermes-agent`'s own git tree,
where `hermes update` (a `git pull`) can overwrite or conflict with a
local patch. This proxy sits outside that tree entirely — nothing about
running it depends on which Hermes version, or even which LLM client, is
calling it.

Verified end-to-end against the real failure: a strict-`json_schema`
request with `max_tokens: 64` (Hermes's actual `title_generator.py`
default) against `ling-3.0-flash-fin-free`, which 400s on the original
request every time. Through the proxy: 8/8 trials returned a correct,
schema-shaped title with `finish_reason: "stop"`, each one via two
automatic repairs stacked (`response_format` dropped, then `max_tokens`
boosted) — tagged in the response as `"_race_proxy": {"repaired_rung":
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
| `timeout` | How long a single race is allowed to run before giving up and returning a `502`. Each backend's internal repair retries share this same budget — see "Structured-output and token-starvation repair" above. If you enable both repairs, a backend can make up to 4 HTTP attempts against one incoming request, so don't set `timeout` too low or you'll cut off a repair mid-retry. 60-90s is a reasonable range. |
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
  on a backend that needs both repairs) — still free on keyless tiers, but
  worth knowing if you're paying per token.
- **No caching.** Every request races from a cold start.
- **No streaming.** Responses get buffered fully before returning. This is
  probably the biggest gap right now, and I'd genuinely welcome a PR for it.
- **No auth on the proxy itself.** It's meant to live on localhost. Put a
  real reverse proxy with real auth in front if you need to expose it.
- **The token-starvation floor is a single constant, not per-model.**
  `MIN_SAFE_MAX_TOKENS` (2000) is a global default across all backends.
  A model with unusually heavy reasoning overhead might still starve at
  2000 — raise the constant if you hit that.
- I tested this by hand against a live free-tier endpoint. There's no
  automated test suite yet, so treat it accordingly until one exists.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
