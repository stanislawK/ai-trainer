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
- ⚠ Unresolved: whether OpenRouter returns cost on every response shape the gateway uses, streaming
  included. The M0 gateway ticket verifies this and records the answer here; until then invariant 6
  may need a follow-up call to the generation endpoint.
