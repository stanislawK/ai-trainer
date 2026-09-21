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
