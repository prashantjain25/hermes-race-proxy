# hermes-race-proxy

A tiny, zero-dependency local HTTP proxy that races multiple OpenAI-compatible
LLM backends against each other and returns whichever finishes first with a
**usable** completion. Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
auxiliary-task routing, but works with any OpenAI-compatible client.

## Why

Hermes Agent's `auxiliary.<task>.fallback_chain` config is **strictly
sequential**: try the primary model, and only on failure/timeout try the next
entry in the chain. That's the right default for most tasks, but for
latency-sensitive auxiliary calls (skill routing, MCP tool selection,
approval judging) that fire on every turn, a sequential retry means you pay
the primary's full timeout before ever trying the fallback.

This proxy instead fires **all configured backends in parallel** and returns
the first one that comes back with real content — cutting worst-case latency
down to whichever backend is fastest *right now*, instead of a fixed
sequential wait.

It was built and tested against a keyless OpenAI-compatible free-tier
endpoint racing two different models, but works with any OpenAI-compatible
`/v1/chat/completions` endpoint — mix free and paid backends, local and
hosted, whatever you want to race. Swap in whichever providers/models you use
in `race_proxy.example.json` (or `.yaml`) — nothing in this repo is tied to a
specific vendor.

## Key design decisions

- **Reasoning-model aware.** Some free-tier models are hidden-reasoning
  models that can burn their entire `max_tokens` budget on invisible
  reasoning and return empty `content` with `finish_reason: "length"`. The
  proxy treats that as **not a valid win** and keeps waiting on the other
  backend(s) rather than serving empty text as if it were a real answer.
  Configure `require_finish_reason: stop` (default) to enforce this.
- **Zero required dependencies.** Pure Python 3 stdlib
  (`http.server`, `concurrent.futures`, `urllib`). YAML config is optional
  (`pip install pyyaml`); JSON config works with no extra installs at all.
- **No new attack surface beyond your existing credentials.** The proxy does
  not store or generate credentials — it forwards whatever `api_key`/headers
  you put in its config, exactly as you would configure any HTTP client. It
  binds to `127.0.0.1` by default and has no auth of its own — do not expose
  it on a public interface without adding one.

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

The response is a normal OpenAI-shape `chat.completion` object, with one
extra non-standard field for observability:

```json
{
  "...": "...",
  "_race_proxy": {"winner": "backend-a", "latency": 1.8}
}
```

## Wiring into Hermes Agent

Point an auxiliary task's `base_url` at the proxy instead of the real
provider:

```yaml
auxiliary:
  skills_hub:
    provider: custom
    base_url: http://127.0.0.1:8977/v1
    model: race-proxy   # ignored by the proxy — it substitutes each
                          # backend's real model name per config
```

Run the proxy as a background service (see `race_proxy.example.json` for the
backend list) before starting Hermes, or manage it with your process
supervisor of choice (systemd, launchd, pm2, etc.).

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
| `host` / `port` | Local bind address for the proxy's HTTP server. |
| `timeout` | Per-race wall-clock budget in seconds. If no backend returns usable content within this window, the proxy returns a `502`. |
| `require_finish_reason` | Set to `null`/omit to accept any completion, even truncated ones. Default `"stop"` rejects `finish_reason: "length"` results (common failure mode with reasoning models given too small a `max_tokens`). |
| `backends[].api_key` | Empty string sends an explicit empty `Authorization` header — required by some keyless free tiers that 401 on *any* recognized bearer token format. |

## Known limitations / honest caveats

- **This doubles (or triples, etc.) your request volume** against whatever
  rate limits your backends enforce. If you're racing two free-tier models
  that share a provider-side rate limit pool, you may hit that limit *faster*
  under sustained load, not slower. Benchmark your actual usage pattern
  before assuming this is a pure win.
- **No response caching.** Every request re-races from scratch.
- **No streaming support yet.** Responses are buffered in full before being
  returned. PRs welcome.
- **No built-in auth.** This is meant for local/trusted-network use. Add a
  reverse proxy with auth in front of it if you need to expose it further.
- Tested manually against a live keyless free-tier OpenAI-compatible
  endpoint as of the initial release; not yet covered by an automated test
  suite. Contributions adding pytest coverage are very welcome.

## License

MIT
