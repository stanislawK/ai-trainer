# ADR 0020 — Decision models (System One)

| | |
|---|---|
| Status | Accepted |
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

**Where Jev is used.** Jev is the default for any step that only picks, wherever it is faster or cheaper than an LLM call or makes a decision the pipeline can't make today. Each use is added with `/add-decision`. It isn't limited to a fixed list (owner-directed amendment, 2026-10-12). The first uses, in M1:
1. **Router fast path.** Jev routes a message only when it holds one confident intent (and sport). Anything multi-request, low-confidence or `unclear` goes to the LLM router, which keeps spans and message order (B14).
2. **Plausible sports for B23.** These are the athlete's sports at or above a probability floor. One plausible sport means no question. Two or more feed the choice card with exactly those sports (ADR-0015).
3. **Safety screen.** It runs in parallel with routing and can only *add* `wellbeing_or_injury` (G3).
4. **Eval check.** A `JevCheck` evaluator grades binary rubric items next to `LLMJudge`.

**Closed-set draft fields.** Jev may fill a draft field whose value comes from a fixed set, such as a climbing activity's kind or style, the sport, or a gym exercise from the plugin's catalogue. Its pick is read against a threshold like any other, and below it, or on a timeout or error, the field keeps the LLM extraction's value. The athlete still sees the draft and confirms it before anything is saved (F2).

**Not for Jev:** reply text, open values (km, grade strings, reps, names), date or load arithmetic. These stay with the LLM or with code.

### Invariants

1. No question, criteria or option text in Python string literals outside tests.
2. `DECISION_MODEL` is a versioned ID; aliases are not allowed.
3. Every decision call by an acting user writes exactly one `llm_calls` row:
   - `template_id` is the decision-set ID, with its version;
   - the model is the one the response names;
   - tokens come from the response usage;
   - `cost` is only what the provider returns, otherwise `NULL`.
4. A decision gates, routes, scores or picks a closed-set draft field. It never writes saved data, and it never produces text the athlete sees.
5. A safety decision can only add the safety intent, never remove or reorder it (ADR-0008 invariant 4).
6. A decision set has a code-graded eval dataset and a committed baseline (with its thresholds) before it becomes active. At least 10 labelled cases suffice for a low-risk decision: one whose wrong answer the athlete sees and can fix before anything is saved (a draft field, a choice card's options), or that costs only a fallback call. A decision that can misroute a message unseen, or that touches safety, needs at least 20.
7. Numbers and dates are computed in code and passed in the state; Jev is never asked to compute.
8. Tests never call Jev (ADR-0013); a fake `DecisionGatewayPort` or a fake `DecisionModel` stands in.
9. Jev's state carries only what the decision needs: the message, recent turns, and the athlete's own training context (their sports, goals, recent sessions). It never carries user identity: no name, email, or user, conversation or message ID. Sending it relies on the data-retention terms the owner accepted in this ADR's Consequences.

## Alternatives considered

- **TypeSafe's own API** (`https://api.typesafe.ai`, `TYPESAFE_API_KEY`). It was this ADR's first choice (2026-10-10). It adds a second vendor, a second secret and a second set of retention terms, for the same model. Dropped on 2026-10-11 once OpenRouter served the TypeSafe wire format.
- **OpenRouter's alpha Decisions API** (`POST /api/alpha/decisions`). Pydantic AI can't reach it: the SDK hardcodes `/v1/systemone` ([pydantic-ai#8552](https://github.com/pydantic/pydantic-ai/issues/8552), open). We would have to own a hand-written client. OpenRouter's `/api/v1/systemone` makes it unnecessary.
- **Replacing the LLM router with Jev.** Jev returns one answer per question. It has no spans or ordered lists of intents, so multi-request messages would regress (B14).
- **`typesafe/jev-router`.** It is a chat meta-router with dynamic pricing, which breaks one model per template and makes `llm_calls` cost meaningless (ADR-0007).

## Consequences

- **Exception to "stable/GA only".** Jev is in early availability. The owner allowed the pinned version in default `Settings` (2026-10-10) because every decision has a fallback (see Decision).
- **Owner-directed amendment, 2026-10-11 (#119).** Jev runs through OpenRouter's TypeSafe-compatible `/api/v1/systemone` instead of TypeSafe's own API. `DECISION_MODEL` became `typesafe/jev-1.13` (was `jev-1.13.0`), and `TYPESAFE_API_KEY` was dropped. Status stays Proposed.
- **Data retention, recorded and accepted by the owner (2026-10-11, #121).** The call crosses OpenRouter and its TypeSafe upstream, and messages carry injury and health text. Terms as published on 2026-10-11:
  - **TypeSafe** ([Data Processing Addendum](https://typesafe.ai/legal/data-processing), updated 2026-04-24). Customer personal data is kept "for as long as necessary taking into account the purpose of the Processing", with no fixed period. Use is limited to the customer's documented instructions (§2.2(b)). The DPA does not mention training, logging, zero data retention or traffic resold through OpenRouter, and its Schedule I lists sensitive data as "N/A". It does give EU SCCs, the UK Addendum, a subprocessor list at trust.typesafe.ai/subprocessors with 15 days to object, and breach notice within 72 hours.
  - **OpenRouter** ([provider logging](https://openrouter.ai/docs/guides/privacy/provider-logging)). Each endpoint's retention and training policy is published as structured data, and an account setting stops routing to providers that may train on prompts. OpenRouter has no routing rule for retention, and the page does not say what OpenRouter itself stores. The `typesafe/jev-1.13` endpoint (`/api/v1/models/typesafe/jev-1.13/endpoints`, checked 2026-10-11) publishes no data policy.
  - **Owner decision.** The owner accepts these gaps for the safety screen and the router fast path: open-ended retention, no clause on training, and health data not named. Only the message text is sent, with no user identity (the state is the bare message). Invariant 9 is satisfied. Extended to every decision on 2026-10-12 (below). Owner action: turn on OpenRouter's training opt-out in the account's privacy settings. Revisit if TypeSafe publishes a retention period or a zero-retention option, or if OpenRouter lists a policy for the endpoint.
- **Cost.** OpenRouter's response carries `usage.cost` (OpenAPI example: `1.9992e-05`), but `typesafe-sdk`'s `Usage` ignores extra fields and `TypeSafeModel.decide` copies only token counts into `RequestUsage` (pydantic-ai-slim 2.55, `models/typesafe.py`, checked 2026-10-11). Decision rows therefore store `cost=NULL`, as invariant 3 and ADR-0018 invariant 6 require, rather than a figure priced in code. The OpenRouter activity page holds the real spend. Revisit if Pydantic AI starts carrying `usage.cost`. Real call at #119 acceptance (2026-10-11, fixture text only): the row named `typesafe/jev-1.13-20260917`, with 560 input and 76 output tokens, `cost=NULL` and 530 ms latency. OpenRouter reports non-zero `output_tokens`, so they are stored as given rather than assumed 0.
- **Telemetry.** `DecisionModel` emits a `decide` span per request. Checked at #119 acceptance with `include_content=False`: no span (`decide`, `chat`, `invoke_agent`) carried the state text (ADR-0018 invariant 4). The `decide` span does carry `pydantic_ai.decision.questions` and `.answers`, which hold the question set's text and Jev's answers, not athlete text.
- **First eval run, #120 (2026-10-11, dataset text only).** `safety_screen` v1 ran through `ai-trainer-evals` on `typesafe/jev-1.13`: 23 cases × 3 repeats. P(pain) barely jitters: most spreads were 0.00–0.01 and the widest was 0.11, on a figurative "pain in the neck". Every pain case scored 0.71 or more and every literal no-pain case 0.16 or less. The owner approved `SAFETY_SCREEN_PAIN_THRESHOLD=0.3` at the acceptance gate. It accepts two false hits, figurative or fatigue wording at 0.43–0.77. The pass rate is 0.891, and the baseline allows a 0.05 drop. Cost and tokens are as #119 recorded: the SDK gives no cost, so the OpenRouter activity page holds the spend.
- **First `router_fast` run, #122 (2026-10-11, dataset text only).** `router_fast` v1 ran on the router's 44 cases × 3 repeats on `typesafe/jev-1.13`, graded as the ADR-0009 amendment of that date describes. Every taken case was routed right, and no case flipped between taken and declined across repeats. The thresholds were picked from the cost of a wrong route, which is worse than the LLM router's call:
  - `ROUTER_FAST_MULTI_REQUEST_THRESHOLD=0.15`. Single-request cases scored 0.14 or less. "Pain ranked first before a log", which would be misrouted, scored 0.21–0.24, so 0.2 left too thin a margin.
  - `ROUTER_FAST_INTENT_THRESHOLD=0.85`. "Adjust plan" (0.89–0.92, right whenever taken) flipped at 0.9. The next case with one request was "prompt injection attempt" at 0.74–0.75, and it is declined.
  - `ROUTER_FAST_SPORT_THRESHOLD=0.9`. Every taken log scored 0.93 or more, including the ambiguous two-sport case, whose acceptable sports the pick matched.
  - Coverage was 0.659 (29 of 44 cases) and accuracy 1.000, against the router baseline's 0.977. Declined cases: several requests or pain with anything else; history-dependent follow-ups (the state is the bare message); `unclear`; and a profile update that Jev read as several requests (P 0.92).
  - Eval calls took 0.7–1.0 s each under the eval's concurrency, well above the 200 ms P50 quoted above. The acceptance run measures the latency on the request path.
- **Plausible sports, #123 (2026-10-11, dataset text only).** Routing carries `plausible_sports`: the athlete's sports with P(sport) at or above `ROUTER_FAST_PLAUSIBLE_SPORT_FLOOR`, most likely first, a sport at exactly the floor included. An athlete with one sport always gets that sport, with no sport question and whatever Jev answered or failed to answer. Otherwise Jev's reading rides along whenever it is sure of one single request, even when the sport is too split to route on: such a log still goes to the LLM router, with the sports left in play. When Jev is unsure, or failed, the sports of the router's own `log_session` intents stand in.
  - **Floor 0.1.** Over 3 repeats on the router's cases, a sport a message clearly rules out scored 0.07 or less, and the lowest sport it leaves in play 0.16 ("Did intervals", gym and cycling: cycling 0.81–0.84, gym 0.16–0.19). A floor of 0.1 sits in that gap, and a miss costs one extra question, not a wrong filing. The baseline records it, and `plausible_accuracy` was 1.000 on the labelled cases.
  - **Jev does not spread on weak wording.** "Did 4×4s on the board" (climbing and gym) scored gym 0.64–0.72, so both sports stay plausible, and the case is labelled so. The ticket's example expected `{climbing}`; no single floor separates it from "did intervals", which needs gym kept at 0.16–0.19. Rewording the sport descriptions (training boards for climbing, interval circuits for gym and cycling) moved the numbers to climbing 0.66–0.73 and gym 0.27–0.34, with intervals at gym 0.28–0.30, and it separates nothing, so v1 stays. "Had a great session today, about 90 minutes" scored gym 0.96 with no sport cue in the message, so that case carries no plausible label. Where weak wording matters, the athlete corrects the guess on the draft card (B23, F2).
  - **Coverage** of the fast path fell from 0.659 to 0.624 on the 47 cases, only because three new cases are declined (a split sport, a sport at 0.65, an intent at 0.84). Accuracy stayed 1.000, against the router's own 1.000 on the same cases.
- **Score answers are a rounded expected level.** For a `score` question, Pydantic AI answers `int(score + 0.5)` from the expected level in `provider_details["scores"]` (pydantic-ai-slim 2.55, `models/decision.py`), not the likeliest level. A split distribution can therefore answer a level that got probability 0: "a friend's injury" answered severity 1 with P(1)=0.0. Evals record the expected level for scores, and a consumer of a score should read `provider_details["scores"]` or the full distribution rather than trust the rounded level near a .5 boundary.
- **Library version.** The repo is locked at `pydantic-ai-slim` 2.46, before the `DecisionModel` rework. #119 raises the floor to 2.55 and adds the `typesafe` extra. The API is moving fast (thresholds were renamed in 2.50), so the port keeps it out of application code.
- **Latency on fallback.** If the fast path falls back, the message pays Jev's latency before the LLM router. A short `decision_call_timeout_seconds` caps that cost. The safety screen runs in parallel with the router, so it adds no wait when the router is slower. When the router answers first, the reply waits for the screen's remaining time, capped by the same timeout (#121).
- **Agent tooling.**
  - `.claude/rules/llm.md` carries these invariants.
  - The new `/add-decision` skill is the checklist for adding a decision set.
  - Decision sets and threshold changes go through `/tune-prompt`.
- **Owner-directed amendment, 2026-10-12: Jev wherever it helps.** Jev is fast (about 200 ms P50) and costs about $0.000015 a call, so the owner wants it used beyond the first three uses.
  - **Uses are open.** "Where Jev is used" no longer lists the only allowed uses. Any step that only picks may use Jev, through `/add-decision`.
  - **Closed-set draft fields.** Jev may fill a draft field from a fixed set (Decision, invariant 4). Open values stay with the LLM or code.
  - **Lighter eval bar for low-risk decisions.** At least 10 labelled cases, against 20 for a decision that can misroute unseen or that touches safety (invariant 6). A baseline with its thresholds is still committed before the set goes live.
  - **Data.** The owner extends the 2026-10-11 retention acceptance above from the safety screen and the router fast path to every decision. The state may carry the message, recent turns and the athlete's own training context, never identity (invariant 9). Re-checked on 2026-10-12: the `typesafe/jev-1.13` endpoint still publishes no data policy (`data_policy: null` at `/api/v1/models/typesafe/jev-1.13/endpoints`), so the gaps accepted on 2026-10-11 are accepted for these uses as well. The revisit triggers stay.
  - **Unchanged.** Every decision keeps a deterministic fallback. `DECISION_MODEL` stays a pinned, versioned ID. A safety decision can only add the safety intent (invariants 2 and 5).
  - The owner accepted this ADR, with this amendment, on 2026-10-12.
- **Candidates for later milestones.** Each is ticketed with its milestone, through `/add-decision`. They are examples, not a closed list:
  - **M1 extraction.** Draft-faithfulness flag on the draft card (F2); gym exercise name mapped onto the plugin catalogue; a climbing activity's kind and style as closed-set draft fields; the ambiguity gate for an extraction that should ask instead (B3, B17, #164); B16 "same session as the last log?".
  - **M3.** Knowledge-chunk rerank and ingestion tagging (ADR-0011).
  - **M4.** Matching a logged session to a planned one; routing "read my planned session".
  - **M5.** Route and crag candidate disambiguation (B18), where low confidence becomes a choice card; trip recommendation score (B21).
- Only `base_url` and the key tie the adapter to OpenRouter, so moving to TypeSafe's own API later is a settings change behind the same port.
- Superseded in part by [ADR-0034](0034-lean-evaluation.md), accepted 2026-10-31: invariant 6. A new set starts at a conservative threshold with a smoke set of at least 5 labelled cases, and its threshold moves later on feedback.
