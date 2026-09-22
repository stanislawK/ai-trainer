# ADR 0018 — Observability and LLM call accounting

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-21 |
| Related | PRD-0001 (G1, G6, G7), ADR-0003, ADR-0004, ADR-0007, ADR-0009, ADR-0013 |

## Context

Every feature in [PRD-0001](../prd/0001-ai-training-companion.md) calls a model. [ADR-0007](0007-llm-integration.md)
invariant 3 requires every call to be recorded with template ID, template version, model, input and
output tokens, cost, latency and outcome — but says nothing about *where*, so the M0 gateway ticket
would have to invent a store. Two different needs hide inside "recorded": durable accounting that is
*queried* (what did this template cost, for which user) and operational tracing that is *looked at*
on a dashboard. [ADR-0009](0009-prompt-evaluation-pipeline.md) declined Logfire and nothing replaced
it, so the second need currently has no answer at all. The project owner asked for the dashboard side
to land in Grafana Cloud.

## Decision

**Accounting — one row per call in PostgreSQL.** An `llm_calls` table (ADR-0004) holds the acting
user, template ID, template version, model, input and output tokens, cost, latency and outcome. It is
written by the gateway adapter behind an application port, never by `llm/` code directly. It is a
user-owned table in the sense of ADR-0004 invariant 1, so account deletion (G7) carries it away.
Calls without an acting user do not take this path: evals write reports (ADR-0009), and admin-run
ingestion records the admin.

**Tracing — OpenTelemetry over OTLP, no vendor SDK.** Pydantic AI v2 emits OpenTelemetry natively:
`Agent.instrument_all(InstrumentationSettings(...))` with a plain `OTLPSpanExporter` and
`BatchSpanProcessor`, no Logfire SDK ([Pydantic AI, *Using OpenTelemetry*](https://github.com/pydantic/pydantic-ai/blob/v2.0.0/docs/logfire.md)).
The provider and exporter are constructed only in the composition root (`main.py`, ADR-0003).

**The backend is configuration.** `Settings` carries `otel_enabled` (default **off**), the OTLP
endpoint, protocol, headers and service name. Grafana Cloud is the intended backend and is reached by
filling those in: its OTLP endpoint speaks OTLP/HTTP with basic auth (account ID plus API token),
supplied through the standard `OTEL_EXPORTER_OTLP_*` variables
([Grafana Cloud, *Send data to the OTLP endpoint*](https://grafana.com/docs/grafana-cloud/send-data/otlp/send-data-otlp/)).
Adopting it is a `.env` change, not a code change, and M0 ships without an account.

**Content stays out of telemetry.** `InstrumentationSettings.include_content` defaults to `True` and
is set to `False`: prompts, completions and tool arguments are the athlete's own words and belong in
the database, not in a third-party trace store.

### Invariants

1. Every model call through the gateway writes exactly one `llm_calls` row, with the acting user, on
   success, timeout and error alike.
2. `llm_calls` has `user_id` NOT NULL, a foreign key to `users` with `ON DELETE CASCADE`, and an index
   (ADR-0004 invariant 1, G7).
3. No telemetry vendor SDK in application code. Only the OpenTelemetry API and SDK, and only the
   composition root constructs a tracer provider or exporter.
4. Spans never carry prompt or completion content, an athlete's message, an email or a key:
   `include_content=False` is set wherever instrumentation is enabled.
5. Telemetry is off by default and off in tests. An unreachable or failing exporter never fails a
   request and never raises into a route.
6. Token counts and cost come from the provider response, never estimated in code.

## Alternatives considered

- **Structured logs only** — no aggregation without a log store; "cost per template last month"
  degrades to grepping stdout.
- **Redis** — not durable queryable storage, and the cost question is a SQL question. It would add a
  service `compose.yaml` does not otherwise need. Redis stays open for per-user rate limiting (B22)
  or caching under its own ADR.
- **The Logfire SDK** — already declined in ADR-0009; it puts a vendor SDK in application code, where
  OTLP is vendor-neutral and Pydantic AI emits it natively.
- **A Grafana Alloy collector in compose** — an extra container for a local POC; the direct OTLP
  endpoint needs none. Revisit if the POC ever grows a fleet of processes.

## Consequences

- M0 splits this across two tickets: the LLM gateway ticket owns the `llm_calls` table and its
  migration; an `infra: export traces over OTLP` ticket owns the instrumentation and the settings.
- `.claude/rules/llm.md` carries invariants 1, 4 and 6; `CLAUDE.md` gains the stack line.
- Deleting a user erases their `llm_calls` rows (G7). Aggregate spend therefore survives only in
  traces, which carry no user ID — an accepted trade of history for G7.
- Resolved by the M0 gateway ticket (#9), two shapes checked separately since only one runs through
  code this project executes:
  - **Non-streaming**, checked against the installed library source (`pydantic-ai` v2.46.0,
    `genai-prices` 0.1.7; no context7/changelog entry covered this distinction): OpenRouter includes a
    real, billed `cost` in the response's `usage` object, but **Pydantic AI v2 does not surface it as
    `result.usage.cost`** — `genai-prices`' OpenRouter extractor
    (`pydantic_ai.models.openrouter._map_openrouter_usage`, backed by `genai_prices/data.py`'s
    `openrouter` provider entry) maps only token counts and never a `cost` key, and
    `pydantic_ai._genai_prices.fill_response_cost` only ever backfills `usage.cost` with a
    `best_effort_price` static-table guess — by its own docstring, no model, OpenRouter included, ever
    sets a real `usage.cost`. The real, billed figure is preserved separately, straight off
    OpenRouter's wire response, on `ModelResponse.provider_details['cost']`
    (`pydantic_ai/models/openrouter.py::_map_openrouter_provider_details`), read via
    `AgentRunResult.response.provider_details`. The gateway reads cost from there, satisfying
    invariant 6.
  - **Streaming**, not exercised by this gateway (it only calls `agent.run()`, never `run_stream()`),
    so checked by reading rather than running: OpenRouter's own docs say `cost` lands in the `usage`
    object of a final, content-free chunk just before `[DONE]`, unconditionally — the
    `usage: {include: true}` / `stream_options.include_usage` toggles are deprecated no-ops now
    ([OpenRouter, *Usage Accounting*](https://openrouter.ai/docs/use-cases/usage-accounting)). On the
    Pydantic AI side, `OpenRouterStreamedResponse._map_provider_details` calls the identical
    `_map_openrouter_provider_details` mapper used for the non-streaming path, so the same extraction
    this gateway relies on applies unchanged once a streaming caller exists. Revisit this bullet with
    a real integration check the day a specialist actually calls `run_stream()`.
- `provider_details['cost']` is per HTTP response, not per run, and a single `agent.run()` call can
  already make more than one model request today — Pydantic AI retries failed output validation once
  by default (`retries` defaults to 1 for both tools and output) — with no tool calls needed. The
  gateway sums `provider_details['cost']` over every `ModelResponse` in `result.all_messages()`
  rather than reading only the last one, so an output-validation retry's cost isn't silently dropped.
  A future specialist with tool calls hits the same accumulation path, already covered by this sum.
