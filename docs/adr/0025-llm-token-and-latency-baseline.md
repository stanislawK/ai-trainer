# ADR 0025 — LLM token and latency baseline

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-18 |
| Related | PRD-0004 (B1, B14, B16, B23), ADR-0007, ADR-0008, ADR-0014, ADR-0018, ADR-0020 |

## Context

Logging a climb takes about half a minute before the draft card appears (B1, B16). The joint plan to cut tokens and latency starts from measured figures, not guesses, so this ADR records them before any fix is picked (#180). They come from the `LLM_DEBUG_LOG` traffic log (ADR-0018, #178) and `make llm-report` (#179).

**Scenario.** On `make start` with `LLM_DEBUG_LOG=true`, signed in as the seeded `scripts/dev_session.py` user, each run in a fresh conversation, 2026-10-18 between 21:09 and 21:19 UTC:

1. "Bouldering at the gym tonight, sent three 6b problems and flashed a 6a" → Confirm
2. "Log this too: after that I climbed two 6b+ boulders and fell twice on a 6c" → Confirm
3. "Also log: finished with four 5c traverses as a cool-down" → Confirm
4. "One more for the log: I tried a 7a project three times without topping", left unconfirmed
5. "Thanks, that's all for today!" (chitchat)

Logs 2–4 were offered as additions to the session (ADR-0014). Because the runs were within the 30-minute merge window, so was log 1 of runs 2 and 3. Run 1 also has two extra messages. First, "Then did two more 6b+ boulders and fell off a 6c twice", which the router took as `edit_session` (not built yet) and which was resent as log 2. Second, "How should I warm up before bouldering?", which Jev routed to `ask_training_question` without an LLM call. Run 1 therefore has 5 router calls and 7 Jev calls. Models: `z-ai/glm-5.3-flash` for the chat templates, `typesafe/jev-1.13` for the decision sets.

**Latency and tokens per template, per run.** These come from `make llm-report`, run separately on each run's lines. Latency is in ms; tokens are the provider's own counts (ADR-0018 invariant 6).

| Template | Calls r1/r2/r3 | p50 ms r1 / r2 / r3 | p95 ms r1 / r2 / r3 | Mean in tok r1 / r2 / r3 | Mean out tok r1 / r2 / r3 |
|---|---|---|---|---|---|
| `router` v2 | 5 / 4 / 4 | 17560 / 17696 / 13521 | 23390 / 21897 / 15987 | 2266 / 2180 / 2234 | 445 / 288 / 368 |
| `climbing.extract` v1 | 4 / 4 / 4 | 13505 / 12603 / 19028 | 16150 / 23664 / 21876 | 1434 / 1381 / 1390 | 658 / 548 / 664 |
| `chitchat` v1 (streamed) | 1 / 1 / 1 | 1357 / 1739 / 2643 | same as p50 (one call) | 710 / 641 / 640 | 159 / 262 / 336 |
| `router_fast` v1 (Jev) | 7 / 5 / 5 | 341 / 342 / 321 | 492 / 418 / 389 | 801 / 802 / 802 | 121 / 121 / 121 |
| `safety_screen` v1 (Jev) | 7 / 5 / 5 | 342 / 324 / 323 | 494 / 413 / 349 | 415 / 416 / 416 | 20 / 20 / 20 |

- Chitchat's time to first text was 1134 / 1491 / 2314 ms.
- The billed cost was 0.001937 / 0.001939 / 0.001728 USD per run. Jev rows carry no cost (ADR-0018, 2026-10-10 amendment).
- In the browser, a log took 19–43 s from send to card, with a median of 35 s over the 10 logs timed. The two Jev calls run in parallel (~0.35 s), then `router` and `climbing.extract` run one after the other. Those two account for almost all of the wait.

**What dominates input.** Size is measured in characters, not tokens, and tokens are never estimated (ADR-0018 invariant 6). The instructions, history and message columns come from `make llm-report`. Its output-schema column is halved here, because the report counts the schema twice. In output mode `auto`, the recorder logs the same schema both as `object` and as the output tool's `parameters`, and `_schema_chars` adds the two together. The model gets only one copy: these replies are `final_result` tool calls, so tool mode was used. The report shows 7228 / 2654 / 3412 / 898 characters for the schema; one copy is half of each. Per-request means over all three runs:

| Template | Instructions | History | Message | Output schema (one copy) | Largest part |
|---|---|---|---|---|---|
| `router` v2 | 3937 | 255 | 133 | 3614 | **instructions, 50%** of 7939 (schema 46%) |
| `climbing.extract` v1 | 2672 | 265 | 135 | 1327 | **instructions, 61%** of 4399 (schema 30%) |
| `chitchat` v1 | 1981 | 650 | 58 | 0 | **instructions, 74%** of 2689 |
| `router_fast` v1 | 0 | 0 | 58 | 1706 | **output schema (the question set), 97%** of 1764 |
| `safety_screen` v1 | 0 | 0 | 58 | 449 | **output schema (the question set), 89%** of 507 |

Per run the parts barely move: history is 210–375 characters for `router` and `climbing.extract`, and 556–839 for `chitchat`. Three more figures from the same lines:

- **The router's schema** holds ten `$defs`, one per intent kind, and each repeats `kind`, `span` and `confidence`.
- **The shared persona preamble** is the 811 characters before `# Task`. It sits in `router` and `climbing.extract` (10% and 18% of their input), though neither template writes to the athlete.
- **Reasoning text** fills most of each glm reply, which is what the output tokens and the wait pay for. It is measured as thinking characters over thinking plus text plus tool-call arguments as sent: 86% for `router` (mean 960 of 1114), 90% for `climbing.extract` (1552 of 1720) and 80% for `chitchat` (796 of 997).

**Timeouts.** None occurred. All 62 lines have `attempt` 1 and no `error`, every call made exactly one request, and the report's `timeouts` and `errors` columns are 0 for every template in every run. The `llm_calls` table agrees, independently of the log: it has 62 rows for the same window, all with outcome `success`. The slowest call took 23664 ms (`climbing.extract`, run 2), which is 79% of `llm_call_timeout_seconds` (30 s). The slowest Jev call took 494 ms (`safety_screen`), 25% of `decision_call_timeout_seconds` (2 s).

**Why the LLM router ran on every log.** `router_fast` picked `log_session` for all 12 logs. The seeded user has no sports set, though ("The athlete's sports: none set yet."). `fast_intents` routes a log on Jev only when the athlete has exactly one sport, or Jev names one of their two or more sports with confidence. With no sports set it always defers to the router. An onboarded athlete with one sport would skip the router whenever Jev clears its gates. In this run `multi_request` was above its 0.15 threshold on 3 of the 12 logs (0.19, 0.18, 0.18). Those three would have gone to the router anyway. These figures therefore show the worst case for routing.

## Decision

Options for the owner, none picked. Each one names the figure it targets.

1. **Limit reasoning on `router` and `climbing.extract`.** Reasoning is 86–90% of their replies, and these two calls are about 30 s of the ~35 s log turn. A reasoning-effort or budget setting per template would cut it, if OpenRouter honours one for this model (unverified, see Consequences).
2. **Flatten the router's output schema.** It is 46% of the router's input. One object with a `kind` enum and an optional `sport`, in place of ten near-identical `$defs`, would carry the same choice.
3. **Drop the persona preamble from templates that never write to the athlete.** For `router` and `climbing.extract` that is 811 characters each, 10% and 18% of their input. The rest of the router's instructions (3126 characters) is the larger part to trim.
4. **Let Jev route logs for an athlete with no sports set.** Ask Jev for the sport among all registered sports, so a confident log skips the ~16 s router call. Thresholds would come from evals (ADR-0020), and the change must be checked against B23.
5. **Start extraction alongside the router when Jev already says `log_session` with one plausible sport.** This saves the router's ~16 s at the risk of a wasted extract call (~0.0002 USD) when the router disagrees.
6. **Try a faster stable model for `router` and `climbing.extract`** through `/tune-prompt`, measured against this baseline.

### Invariants

1. The figures in Context are the baseline. A ticket that implements an option reruns this scenario with `make llm-report` and compares against them in its own PR. It never edits them here.
2. An option that changes a prompt, model, schema or the router goes through `/tune-prompt` with its eval (ADR-0009). A Jev threshold changes only through evals (ADR-0020).

## Alternatives considered

- **Cap or summarise history.** History is 255–650 characters per request, at most 24% of any template's input, so capping it saves little at this size. Revisit once conversations grow long.
- **Measure through traces.** Spans carry no content (ADR-0018 invariant 4), so they can't show what fills a prompt. The local traffic log can.

## Consequences

- **Unverified, flagged:** whether OpenRouter exposes a reasoning switch or budget for `z-ai/glm-5.3-flash`, and whether Pydantic AI v2 passes it through. Option 1 has to check this with context7 and the OpenRouter model page before it is planned.
- **Report defect, its own ticket:** `ai-trainer llm-debug-report` (#179) doubles the output-schema column in output mode `auto` (see Context). Its fix is to count `object` or the tools' `parameters`, never both. Until then, halve that column when comparing against this baseline.
- **Found during the runs, its own ticket:** "Then did two more 6b+ boulders…" was routed to `edit_session`. The router's reasoning read the history's "Check it before it's saved" as an unsaved draft, because the recent messages don't show that the draft was confirmed. Rendering the confirmed state into history is one candidate fix, which a ticket would decide. The message is also a candidate router eval case.
- **Found during the runs, an eval case:** the 7a project was drafted as Lead in runs 1 and 3 and as Boulder in run 2.
- **Re-baseline needed:** this baseline uses an athlete with no sports. A second run with an onboarded single-sport athlete would show the routing that most athletes get.
- Picking an option is the owner's call. It then becomes a ticket via `/create-tickets`, and this ADR is accepted or superseded by the one that records the choice.
