Use the OpenRouter MCP server (`mcp__openrouter__*`, declared in `.mcp.json`) whenever a task needs facts about OpenRouter models, upstream providers or our own OpenRouter usage: choosing or comparing a model, choosing provider routing, explaining a slow or failing call, or checking a price. Prefer it over memory and web search for these facts, because the catalogue and the endpoint stats change daily. Use context7 for library APIs (Pydantic AI's `OpenRouterModelSettings`), not this server.

Do not use it for: running the app's model traffic (the app calls the OpenRouter API through `OpenRouterGateway`, ADR-0007), evals (`uv run ai-trainer-evals`, through `/tune-prompt`), or anything on the production host (ADR-0023).

## Tools

| Need | Tool |
|---|---|
| Shortlist models by price, benchmarks, tool-call success, supported parameters or region | `list-models` (server-side `sort` and filters) |
| One model's details, including its `reasoning` block (mandatory? supported efforts? default effort?) | `get-model` |
| Upstreams for a model: price, quantization, supported parameters, and latency and throughput percentiles over the last 30 minutes | `list-model-endpoints` |
| Which upstream degraded during a window (e.g. a reported timeout) | `get-endpoint-uptime-history` |
| Third-party quality scores (Artificial Analysis indexes) | `list-benchmarks` |
| Exact cost, tokens and serving provider of one generation | `get-generation` |
| OpenRouter how-to (routing, caching, reasoning, regions) | `search-docs` |
| Provider slugs for `order` / `only` / `ignore` | `list-providers` |

## Rules

1. **Read-only by default.** `send-message`, `send-batch`, `generate-image`, `generate-speech` and `transcribe-audio` cost money. `send-feedback` writes to OpenRouter. Call them only when the owner asks in this session, and never with athlete data: no message text, profile, history or identity.
2. **Cite what you read.** Endpoint stats are a 30-minute window and benchmarks drift. When a figure goes into a doc, ticket or PR, give the tool and the date (e.g. "`list-model-endpoints`, 2026-10-19").
3. **Benchmarks only screen.** Artificial Analysis indexes rank general ability. A model or routing change is decided on this project's own evals and the latency scenario (ADR-0009, ADR-0026), through `/tune-prompt`.
4. **Defaults stay stable.** Only stable/GA model IDs become `Settings` defaults (`CLAUDE.md`). Preview, `-exp` or days-old snapshots are noted and skipped.
5. **Reasoning is per model.** Check `get-model`'s `reasoning` block before relying on `effort: none`: where reasoning is mandatory, OpenRouter silently ignores `none`.

## Setup

Already declared in `.mcp.json`. Each developer authorizes it once with `/mcp` → openrouter → Authenticate, or `claude mcp login openrouter`. The OAuth key expires after 7 days and carries a spend cap set on the approval screen ([docs](https://openrouter.ai/docs/guides/overview/mcp-server)). Without authorization the tools are unavailable: say so and fall back to `https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints`, which has the catalogue and prices but no live latency figures.
