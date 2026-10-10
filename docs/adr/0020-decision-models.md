# ADR 0020 — Decision models (System One)

| | |
|---|---|
| Status | Proposed |
| Date | 2026-10-10 |
| Related | PRD-0003 (G1, G3, G6, B3, B14, B17, B23), ADR-0002, ADR-0007, ADR-0008, ADR-0009, ADR-0013, ADR-0015, ADR-0018 |

## Context

Several steps in the pipeline only *pick*, they don't write. Classifying a message (B14) is one, inferring its sport (B23) another. Deciding whether to ask the athlete (B3, B17) and spotting pain (G3) are picks too. Today the router does all of this through a chat model and returns a confidence that nothing uses (ADR-0008, #82 amendment). Every message also waits for that call before its first token.

TypeSafe **Jev** is a non-generative "System One" model. You send it a text *state* and typed questions. Each answer is either a yes/no probability, one option with the full distribution (at most 255 options), or a level on an ordered rubric. It writes no text.

- **Price:** $0.042 per 1M input tokens; output is free. A 350-token call costs about $0.000015.
- **Speed:** P50 is about 200 ms.
- **Limits:** a 32k-token state.
- **Snapshot:** `jev-1.13-20260917`.

Sources: [OpenRouter model page](https://openrouter.ai/typesafe/jev-1.13) and [What is Jev](https://openrouter.ai/blog/insights/what-is-jev/).

Jev is calibrated *in aggregate* only:
- Probabilities jitter between calls; the same input gave 0.79 and then 0.84.
- `confidence` describes how spread out the probabilities are, not whether the answer is right.
- Thresholds therefore have to come from labelled data, and the model must stay pinned while they hold.
- It does no arithmetic, date math or image input.
- Field names are not sent to the model, so every bit of meaning has to be in the descriptions.

Pydantic AI v2 supports it natively as `TypeSafeModel` ([docs](https://github.com/pydantic/pydantic-ai/blob/main/docs/models/decision.md)):
- It was added in v2.45.
- In v2.50 it was rebuilt on a generic `DecisionModel`, and `typesafe_*` settings became `decision_*`.
- The output type supplies the wording:
  - `Field(description=…)` or a field docstring is the question;
  - `BoolCriteria(true=…, false=…)` is a yes/no's criteria;
  - `UseEnumMemberDocstrings` on an `Enum` gives one description per option;
  - an `IntEnum` with levels 0..n is a rubric;
  - runtime `Choices({...})` builds a pick-one at runtime;
  - a bounded `float` returns P(yes) itself.
- Distributions come back in `ModelResponse.provider_details` (`confidence`, `probabilities`, `scores`).

## Decision

**Call path.** Jev runs through Pydantic AI `TypeSafeModel` (`pydantic-ai-slim[typesafe]`, which pulls in `typesafe-sdk`). It calls TypeSafe's own API, not OpenRouter.
- The provider is built as `TypeSafeProvider(api_key=settings.typesafe_api_key)`, so `Settings` stays the only reader of the environment (ADR-0002).
- The application uses Jev only through `DecisionGatewayPort.decide(...)`. The adapter lives in `src/ai_trainer/llm/` and reuses `OpenRouterGateway`'s timeout, shielded `llm_calls` write and outcome mapping. Those parts are extracted into a shared helper, not copied.

**Model.** `DECISION_MODEL` holds a versioned ID, `jev-1.13.0` today. It is never `jev-latest` or `jev-preview`, because a threshold is tuned against one version. The secret `TYPESAFE_API_KEY` lives in `.env`.

**Question sets are files.** Each set is `src/ai_trainer/llm/decisions/<id>/v<N>.en.yaml` and holds:
- the goal;
- one entry per question: its kind (`probability`, `choice` or `score`), the question text, and the criteria, option descriptions or ordered levels.

A loader builds the Pydantic output type at runtime, the way `llm/router.py` builds `RouterOutput`. Option sets that depend on the athlete, such as their sports, are filtered at runtime from descriptions held in the file. Sets are versioned and evaluated like prompt templates (ADR-0008, ADR-0009).

**Reading answers.**
- Gates read the probability, from a bounded `float` field or `provider_details`, and compare it with a threshold in `Settings`. They never use the library's default 0.5 cut for `bool`.
- Choices read the full distribution, keyed by option name and never by position.
- Thresholds are calibrated on the set's eval dataset and recorded in its baseline.

**Fallback.** Every decision has a deterministic fallback, used on timeout, on an error, and on any answer below its threshold. The fallback is either the existing LLM path or the safe side of the gate.

**Uses in M1:**
1. **Router fast path.** Jev routes a message only when it holds one confident intent (and sport). Anything multi-request, low-confidence or `unclear` goes to the LLM router, which keeps spans and message order (B14).
2. **Plausible sports for B23.** These are the athlete's sports at or above a probability floor. One plausible sport means no question. Two or more feed the choice card with exactly those sports (ADR-0015).
3. **Safety screen.** It runs in parallel with routing and can only *add* `wellbeing_or_injury` (G3).
4. **Eval check.** A `JevCheck` evaluator grades binary rubric items next to `LLMJudge`.

**Not for Jev:** reply text, extracted values (km, grades, reps), date or load arithmetic. These stay with the LLM or with code.

### Invariants

1. No question, criteria or option text in Python string literals outside tests.
2. `DECISION_MODEL` is a versioned ID; aliases are not allowed.
3. Every decision call by an acting user writes exactly one `llm_calls` row:
   - `template_id` is the decision-set ID, with its version;
   - the model is the one the response names;
   - tokens come from the response usage;
   - `cost` is only what the provider returns, otherwise `NULL`.
4. A decision gates, routes or scores. It never writes data, and it never produces text the athlete sees.
5. A safety decision can only add the safety intent, never remove or reorder it (ADR-0008 invariant 4).
6. A decision set has a code-graded eval dataset and a committed baseline (with its thresholds) before it becomes active.
7. Numbers and dates are computed in code and passed in the state; Jev is never asked to compute.
8. Tests never call TypeSafe (ADR-0013); a fake `DecisionGatewayPort` or `FunctionModel` stands in.
9. No athlete text is sent to TypeSafe until the owner records TypeSafe's data-retention terms in this ADR's Consequences.

## Alternatives considered

- **OpenRouter Decisions API** (`POST /api/alpha/decisions`). It keeps one vendor and is on OpenRouter's [zero-data-retention list](https://openrouter.ai/docs/features/zdr). Pydantic AI can't reach it yet: the SDK hardcodes `/v1/systemone` ([pydantic-ai#8552](https://github.com/pydantic/pydantic-ai/issues/8552), open). We would have to own a hand-written client. The owner chose `TypeSafeModel`.
- **Replacing the LLM router with Jev.** Jev returns one answer per question. It has no spans or ordered lists of intents, so multi-request messages would regress (B14).
- **`typesafe/jev-router`.** It is a chat meta-router with dynamic pricing, which breaks one model per template and makes `llm_calls` cost meaningless (ADR-0007).

## Consequences

- **Exception to "stable/GA only".** Jev is in early availability. The owner allowed the pinned version in default `Settings` (2026-10-10) because every decision has a fallback (see Decision).
- **⚠ Data retention, unresolved.** TypeSafe's own retention and privacy terms could not be found on 2026-10-10. Messages carry injury and health text, so invariant 9 blocks the fast path and the safety screen until the owner records the terms here. The eval check sends only dataset text.
- **Cost.** The `DecisionModel` source maps token usage but no cost (`pydantic_ai/models/decision.py`, checked 2026-10-10), so decision rows are expected to store `cost=NULL`. Usage reports `output_tokens` as 0. The first ticket checks both against a real response and records the result here.
- **Telemetry.** `DecisionModel` emits a `decide` span per request. The first ticket checks that, with `include_content=False`, the span carries no state text (ADR-0018 invariant 4).
- **Library version.** The repo is locked at `pydantic-ai-slim` 2.46, before the `DecisionModel` rework. The first ticket raises the floor to the current 2.x (2.55 on 2026-10-10) and adds the `typesafe` extra. The API is moving fast (thresholds were renamed in 2.50), so the port keeps it out of application code.
- **Latency on fallback.** If the fast path falls back, the message pays Jev's latency before the LLM router. A short `decision_call_timeout_seconds` caps that cost. The safety screen runs in parallel, so it adds no wait.
- **Agent tooling.**
  - `.claude/rules/llm.md` carries these invariants.
  - The new `/add-decision` skill is the checklist for adding a decision set.
  - Decision sets and threshold changes go through `/tune-prompt`.
- **Candidates for later milestones.** Each is ticketed with its milestone, through `/add-decision`:
  - **M1 extraction.** Draft-faithfulness flag on the draft card (F2); gym exercise name mapped onto the plugin catalogue; B16 "same session as the last log?".
  - **M3.** Knowledge-chunk rerank and ingestion tagging (ADR-0011).
  - **M4.** Matching a logged session to a planned one; routing "read my planned session".
  - **M5.** Route and crag candidate disambiguation (B18), where low confidence becomes a choice card; trip recommendation score (B21).
- If pydantic-ai#8552 lands, the OpenRouter path can replace TypeSafe's API behind the same port with no application change.
