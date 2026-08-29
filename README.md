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
      "headers": {}
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `host` / `port` | Where the proxy's HTTP server listens. |
| `timeout` | How long a single race is allowed to run before giving up and returning a `502`. |
| `require_finish_reason` | Set to `null` or leave it out to accept any completion, truncated or not. Default `"stop"` rejects `finish_reason: "length"` results, which is the usual failure mode when a reasoning model gets too small a `max_tokens`. |
| `backends[].api_key` | An empty string sends an explicit empty `Authorization` header. Some keyless free tiers 401 you the moment they see any recognized bearer token format, even a made-up one, so this matters. |

## Things I haven't solved yet

- **You're now sending 2x, 3x, however many requests you're racing.**
  If two of your backends share a rate limit, racing them can burn through
  that limit faster, not slower. Worth watching before you assume this is
  a clean win for your setup.
- **No caching.** Every request races from a cold start.
- **No streaming.** Responses get buffered fully before returning. This is
  probably the biggest gap right now, and I'd genuinely welcome a PR for it.
- **No auth on the proxy itself.** It's meant to live on localhost. Put a
  real reverse proxy with real auth in front if you need to expose it.
- I tested this by hand against a live free-tier endpoint. There's no
  automated test suite yet, so treat it accordingly until one exists.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
