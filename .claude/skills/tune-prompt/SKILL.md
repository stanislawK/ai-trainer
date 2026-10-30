---
name: tune-prompt
description: Change a prompt template, a Jev decision set or its thresholds, a model ID, provider routing or the router — update the smoke set from real chats, make the change, pass the smoke set, and show the run at the review gate. Full eval runs only on demand.
argument-hint: "[template-id]"
disable-model-invocation: true
---

# Tune prompt: $ARGUMENTS (ADR-0008, ADR-0020, ADR-0034)

Smoke runs call real models through OpenRouter. They cost cents, but say so before the first run.

The same loop covers a **Jev decision set** (`src/ai_trainer/llm/decisions/<id>/v<N>.en.yaml`):
- Its cases carry expected labels.
- The change is a new `v<N+1>.en.yaml`, a `DECISION_MODEL` change or a threshold change in `Settings`.
- A new set starts at a conservative threshold. A threshold moves on feedback (`uv run ai-trainer feedback-report`), with the counts quoted in the PR.

## Smoke loop (every change)

1. **Cases from real chats.** Update `evals/smoke/<template-id>.yaml`, about 5–10 cases.
   - Cut them from `evals/field/` transcripts first, keep the athlete's wording as typed, and set `metadata.field` to the transcript.
   - Add a synthetic case only for a reported bug or a safety rule.
   - Imperial units and V/YDS grades are best effort (G15): don't add cases for them.
   - Cases can come with the change; they don't have to come first.
2. **Change.** Write the new version as `src/ai_trainer/llm/prompts/<template-id>/v<N+1>.en.md`, or change the model ID, provider routing or reasoning effort in `Settings`.
   - Never edit a version that has a committed baseline.
   - For a model or routing change, shortlist candidates with the OpenRouter MCP first (`.claude/rules/openrouter-mcp.md`): stable/GA, the parameters the template needs, the `reasoning` block from `get-model`, and price and live latency from `list-model-endpoints`.
   - Record why each candidate was picked or dropped (ADR-0026).
3. **Run.** `uv run ai-trainer-evals smoke <template-id>` runs the app's version once per case, code-graded with no judge.
   - Every case passes, or the case is listed under `known_gaps` with a ticket number.
   - When the change touches routing, replies or safety, also run the safety set (`smoke safety`). It runs with its judge and must pass.
4. **Show.** Paste the run's summary into the ticket's acceptance script and PR. The review gate is the approval; there is no separate promote gate.

## Full run (on demand only)

Run the full suite only for:
- a model swap;
- a release the owner wants checked;
- when the owner asks.

Steps:
1. **Run.** `uv run ai-trainer-evals run <template-id> [--version N]` runs the existing datasets with repeats and `LLMJudge`, and diffs against the committed baseline.
2. **Report.** Report regressions per grader and per case, with imperial and V/YDS regressions on their own line. A change aimed at latency also reports p50, p95 and USD per log turn from the latency scenario (ADR-0026).
3. **Promote.** `--promote` writes `evals/baselines/<template-id>.json` as a reference point, never as a gate.

The judge model is set explicitly with `set_default_judge_model(OpenRouterModel(settings.eval_judge_model))` and must differ from the model under test.

Until the `smoke` command exists (M1e), a change runs `uv run ai-trainer-evals run <template-id>` on the existing dataset once and shows the result at the review gate, without a promote.
