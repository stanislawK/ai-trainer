# ADR 0007 — LLM integration

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (G2, G6, G10, B1, B8, B18), ADR-0008, ADR-0009, ADR-0010, ADR-0011, ADR-0016 |

## Context

Every core feature uses language models: extraction (B1), answers (B8), planning (B9–B11). The project owner chose OpenRouter as the single gateway for text and embeddings, and strong typing throughout.

## Decision

- **OpenRouter is the only gateway**, for chat models and embeddings.
- **Pydantic AI v2** runs all agents: `pydantic_ai.models.openrouter.OpenRouterModel` with `OpenRouterProvider(api_key=settings…)`.
- Agents are typed. `deps_type` is a per-request context (acting user, profile snapshot, …); `output_type` is a Pydantic model.
- Each prompt template has its own model ID, set in settings: a small, fast model for routing and stronger models for specialists (ADR-0008).
- The persona is sent as static instructions, which Pydantic AI places before dynamic ones. That makes it a cacheable prefix: enable `OpenRouterModelSettings(openrouter_cache_instructions=True)`.
- The application layer uses LLM features through ports; Pydantic AI code lives in `src/ai_trainer/llm/`.
- **Web search** uses Pydantic AI's `WebSearchTool` via `capabilities=[NativeTool(...)]`, only in specialists that need it: Q&A, and the route and area specialists of ADR-0016, whose searches are constrained to one domain and whose results are cached. Results are labeled as web in the reply.
- **Embeddings** go through one adapter calling OpenRouter `POST /api/v1/embeddings` in batches (ADR-0011).
- Every call has a timeout. A failure reaches the user as a friendly chat message, never as a stack trace.

### Invariants

1. No model ID or API key appears in code; both come from `Settings`.
2. Chat-model calls go through Pydantic AI only; embeddings go through the single embeddings adapter.
3. Every LLM call is recorded with template ID, template version, model, input and output tokens, cost, latency and outcome.
4. Content from the web is always labeled as web in the reply.
5. The test suite never calls a real model (ADR-0013).

## Consequences

- ⚠ OpenRouter has deprecated its `web` plugin and the `:online` suffix in favour of the `openrouter:web_search` server tool (the model decides when to search, capped by `max_total_results`). Pydantic AI's docs still say its OpenRouter adapter "uses plugins". Check what it sends at M0; if it's still the plugin, send the server tool instead and record that here.
- `.claude/rules/llm.md` carries these rules.
- Client lifecycle gotcha, found at #9: `OpenRouterGateway` (`src/ai_trainer/llm/gateway.py`) builds one `OpenRouterProvider` — and with it, one long-lived `AsyncOpenAI`/httpx client — at construction and reuses it for every call, which is correct for connection pooling but means nothing ever closes it. #9 doesn't wire the gateway into the composition root (no consumer exists yet), so this doesn't bite today. Whichever ticket first constructs `OpenRouterGateway` in `main.py` must close its client (`gateway._provider.client.close()`, or expose a small `aclose()` on the gateway) from a FastAPI shutdown hook, the same way `adapters/db.py`'s engine needs `dispose()`.
- Gateway/registry integration gap, found at #11: `PromptRegistry.build_agent` (`src/ai_trainer/llm/prompts/registry.py`) composes persona + template into an `Agent` directly, independent of `OpenRouterGateway` — it never goes through `OpenRouterGateway.run`, so nothing calling it today gets `llm_calls` accounting, the timeout wrapper or friendly-error translation this ADR's invariants 1–3 require. #11 ships no consumer (no real specialist template exists before M1), so this doesn't bite today. Whichever ticket first calls `build_agent` in production (the M1 router) must decide how its output reaches those guarantees — e.g. by having the caller resolve the model from `Settings` via `template.model_settings_key` and pass it to the built agent's `run`, with the caller itself (or an extended gateway) doing the accounting/timeout/error-translation `OpenRouterGateway.run` currently does only for gateway-issued calls.
- Streaming, added at #79: `OpenRouterGateway.stream` (`LlmGatewayPort.stream`) yields `TextChunk`s and then one `StreamEnd` carrying the same `LlmGatewayResult` shape as `run()`, with the same friendly errors. Its timeout is per wait (first response, then each chunk), not a deadline on the whole reply, so a long answer that keeps flowing is not cut off. Pydantic AI's `run_stream` holds an anyio cancel scope that must be entered and left in one task, so a producer task owns the model stream and the generator only reads a queue; holding the scope inside the generator broke an unclosed `break` or a cancelled consumer, whose generator is finalised from another task. That way the row is written even then, though for an abandoned generator only when the event loop finalises it, so prefer `contextlib.aclosing`. `stream_text()` also debounces chunks by 0.1 s unless `debounce_by=None`. `stream()` takes no `output_type` — replies are plain text; a structured stream is a separate decision. A reply that ends with no text at all is an `error` row. A failing `llm_calls` write propagates, as in `run()`.
- Owner-directed amendment, 2026-10-10 ([ADR-0020](0020-decision-models.md)): **decision calls** are a third call kind, next to chat and embeddings. TypeSafe Jev runs through Pydantic AI `TypeSafeModel` against TypeSafe's own API, behind `DecisionGatewayPort`. OpenRouter stays the only gateway for chat models and embeddings. Invariants 1, 3 and 5 apply unchanged; invariant 2 is met because decision calls also go through Pydantic AI.
- Upstream pinning, found at #127 and chosen by the owner, 2026-10-11: OpenRouter served `z-ai/glm-5.3-flash` from 33 endpoints of mixed fp4/fp8 quantization ([endpoints listing](https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints)). Each eval call could land on a different one, so one prompt routed the same message differently from call to call, and temperature 0 did not help. An optional `ROUTER_PROVIDER` setting now pins the router's upstream through `openrouter_provider` (`order: [slug]`, fallbacks allowed), set by `OpenRouterGateway.run` and by the eval CLI alike (`pinned_upstream` in `src/ai_trainer/llm/gateway.py`). A `--model` override runs unpinned. With `ROUTER_PROVIDER=z-ai`, router v2 went from 0.886 to 0.977 assertions. One model per template is unchanged.
