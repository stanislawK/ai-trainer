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

**Call path.** Jev runs through Pydantic AI `TypeSafeModel` (`pydantic-ai-slim[typesafe]`, which pulls in `typesafe-sdk`), against OpenRouter. OpenRouter serves `POST /api/v1/systemone`, which it documents as "compatible with the TypeSafe SDKs" ([OpenRouter OpenAPI](https://openrouter.ai/openapi.json), `createSystemone`). The SDK appends `/v1/systemone` to its base URL itself.
- The provider is built as `TypeSafeProvider(base_url="https://openrouter.ai/api", api_key=settings.openrouter_api_key)`. OpenRouter stays the one vendor and the one key, and `Settings` stays the only reader of the environment (ADR-0002).
- The application uses Jev only through `DecisionGatewayPort.decide(...)`. The adapter lives in `src/ai_trainer/llm/` and reuses `OpenRouterGateway`'s timeout, shielded `llm_calls` write and outcome mapping. Those parts are extracted into a shared helper, not copied.

**Model.** `DECISION_MODEL` holds OpenRouter's versioned ID, `typesafe/jev-1.13` today. It is never an alias (`~typesafe/jev-latest`, `jev-latest`, `jev-preview`), because a threshold is tuned against one version. The response names the dated snapshot (`typesafe/jev-1.13-20260917`), which is what the `llm_calls` row stores. No extra secret: the call uses `OPENROUTER_API_KEY`.

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
8. Tests never call Jev (ADR-0013); a fake `DecisionGatewayPort` or a fake `DecisionModel` stands in.
9. No athlete text is sent to Jev until the owner records the data-retention terms that apply to it (OpenRouter's and the TypeSafe upstream's) in this ADR's Consequences.

## Alternatives considered

- **TypeSafe's own API** (`https://api.typesafe.ai`, `TYPESAFE_API_KEY`). It was this ADR's first choice (2026-10-10). It adds a second vendor, a second secret and a second set of retention terms, for the same model. Dropped on 2026-10-11 once OpenRouter served the TypeSafe wire format.
- **OpenRouter's alpha Decisions API** (`POST /api/alpha/decisions`). Pydantic AI can't reach it: the SDK hardcodes `/v1/systemone` ([pydantic-ai#8552](https://github.com/pydantic/pydantic-ai/issues/8552), open). We would have to own a hand-written client. OpenRouter's `/api/v1/systemone` makes it unnecessary.
- **Replacing the LLM router with Jev.** Jev returns one answer per question. It has no spans or ordered lists of intents, so multi-request messages would regress (B14).
- **`typesafe/jev-router`.** It is a chat meta-router with dynamic pricing, which breaks one model per template and makes `llm_calls` cost meaningless (ADR-0007).

## Consequences

- **Exception to "stable/GA only".** Jev is in early availability. The owner allowed the pinned version in default `Settings` (2026-10-10) because every decision has a fallback (see Decision).
- **Owner-directed amendment, 2026-10-11 (#119).** Jev runs through OpenRouter's TypeSafe-compatible `/api/v1/systemone` instead of TypeSafe's own API. `DECISION_MODEL` became `typesafe/jev-1.13` (was `jev-1.13.0`), and `TYPESAFE_API_KEY` was dropped. Status stays Proposed.
- **⚠ Data retention, unresolved.** The call now crosses OpenRouter and its TypeSafe upstream. Neither's terms for this endpoint are recorded yet. Messages carry injury and health text, so invariant 9 blocks the fast path and the safety screen until the owner records the terms here. The eval check sends only dataset text.
- **Cost.** OpenRouter's response carries `usage.cost` (OpenAPI example: `1.9992e-05`), but `typesafe-sdk`'s `Usage` ignores extra fields and `TypeSafeModel.decide` copies only token counts into `RequestUsage` (pydantic-ai-slim 2.55, `models/typesafe.py`, checked 2026-10-11). Decision rows therefore store `cost=NULL`, as invariant 3 and ADR-0018 invariant 6 require, rather than a figure priced in code. The OpenRouter activity page holds the real spend. Revisit if Pydantic AI starts carrying `usage.cost`. Real call at #119 acceptance (2026-10-11, fixture text only): the row named `typesafe/jev-1.13-20260917`, with 560 input and 76 output tokens, `cost=NULL` and 530 ms latency. OpenRouter reports non-zero `output_tokens`, so they are stored as given rather than assumed 0.
- **Telemetry.** `DecisionModel` emits a `decide` span per request. Checked at #119 acceptance with `include_content=False`: no span (`decide`, `chat`, `invoke_agent`) carried the state text (ADR-0018 invariant 4). The `decide` span does carry `pydantic_ai.decision.questions` and `.answers`, which hold the question set's text and Jev's answers, not athlete text.
- **First eval run, #120 (2026-10-11, dataset text only).** `safety_screen` v1 ran through `ai-trainer-evals` on `typesafe/jev-1.13`: 23 cases × 3 repeats. P(pain) barely jitters: most spreads were 0.00–0.01 and the widest was 0.11, on a figurative "pain in the neck". Every pain case scored 0.71 or more and every literal no-pain case 0.16 or less. The owner approved `SAFETY_SCREEN_PAIN_THRESHOLD=0.3` at the acceptance gate. It accepts two false hits, figurative or fatigue wording at 0.43–0.77. The pass rate is 0.891, and the baseline allows a 0.05 drop. Cost and tokens are as #119 recorded: the SDK gives no cost, so the OpenRouter activity page holds the spend.
- **Score answers are a rounded expected level.** For a `score` question, Pydantic AI answers `int(score + 0.5)` from the expected level in `provider_details["scores"]` (pydantic-ai-slim 2.55, `models/decision.py`), not the likeliest level. A split distribution can therefore answer a level that got probability 0: "a friend's injury" answered severity 1 with P(1)=0.0. Evals record the expected level for scores, and a consumer of a score should read `provider_details["scores"]` or the full distribution rather than trust the rounded level near a .5 boundary.
- **Library version.** The repo is locked at `pydantic-ai-slim` 2.46, before the `DecisionModel` rework. #119 raises the floor to 2.55 and adds the `typesafe` extra. The API is moving fast (thresholds were renamed in 2.50), so the port keeps it out of application code.
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
- Only `base_url` and the key tie the adapter to OpenRouter, so moving to TypeSafe's own API later is a settings change behind the same port.
