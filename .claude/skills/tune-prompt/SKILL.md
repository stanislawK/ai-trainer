---
name: tune-prompt
description: Eval-driven change to a prompt template, a model ID or the router — add cases first, write a new template version, compare against the committed baseline, then promote after a human gate.
argument-hint: "[template-id]"
disable-model-invocation: true
---

# Tune prompt: $ARGUMENTS (ADR-0008, ADR-0009)

Evals call real models through OpenRouter and cost money. Say so before the first run.

1. **Cases first.** Add or extend cases in `evals/datasets/<template-id>.yaml` for the behaviour you want: expected outputs for code-graded checks, rubrics for `LLMJudge`. Before finalizing, fan out: have several agents each propose a handful of edge cases from a different angle (ambiguous input, adversarial/safety, unit or locale edge cases, multi-intent messages). Merge and dedupe the proposals, then keep the diverse subset that actually goes into the dataset.
2. **Baseline.** Run `uv run ai-trainer-evals run <template-id>` on the current active version to see where it stands with the new cases.
3. **Change.** Write the new version as `src/ai_trainer/llm/prompts/<template-id>/v<N+1>.en.md` (or change the model ID in settings). Never edit a version that has a committed baseline.
4. **Compare.** Run the new version and compare against the baseline per grader and per case. Investigate every regression.
5. **HUMAN GATE.** Show the comparison, then `AskUserQuestion`: Approve / Request changes / Reject.
6. **Promote** (after Approve): switch the active version in the registry and commit the new baseline `evals/baselines/<template-id>.json` in the same change.

The judge model is set explicitly with `set_default_judge_model(OpenRouterModel(settings.eval_judge_model))` and must differ from the model under test.
