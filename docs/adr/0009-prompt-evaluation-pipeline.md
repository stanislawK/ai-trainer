# ADR 0009 — Prompt evaluation pipeline

| | |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Related | PRD-0001 (G2, G3, G6, B1, B8), ADR-0007, ADR-0008, ADR-0013 |

## Context

Unit tests with fake models prove the plumbing but say nothing about the quality of real model output. G6 requires every AI capability to meet a quality bar, and prompts, models and the router will change often. The project owner asked for an internal evaluation pipeline, modelled on the three grading types taught in Anthropic's prompt-evaluation course.

## Decision

- **Pydantic Evals** (`Dataset`, `Case`, `Evaluator`, `LLMJudge`, `EvaluationReport`; built-ins such as `EqualsExpected`, `IsInstance`, `Contains`, `MaxDuration`). It comes from the Pydantic AI ecosystem. The course's promptfoo is Node-based, so it is not used; the toolchain stays in Python.
- **Grading types:**
  - *Code-graded* — custom evaluators: router label accuracy, field-level match of extracted payloads, schema validity.
  - *Model-graded* — `LLMJudge` rubrics for empathy and tone (G2), helpfulness, safety handling (G3), citation faithfulness (B8).
  - *Human-graded* — sampled spot checks; an internal review page later.
- **Judge model:** pinned in settings, different from the model under test, temperature 0, and set explicitly on every run with `set_default_judge_model(OpenRouterModel(settings.eval_judge_model))`. Otherwise `LLMJudge` falls back to its built-in default (`openai:gpt-5.2`) and bypasses OpenRouter.
- **Layout:**

  ```
  evals/
    datasets/<template_id>.yaml    # committed; Dataset.to_file / Dataset.from_file
    baselines/<template_id>.json   # committed; version, model, averages, per-case scores, thresholds
    reports/                       # gitignored
  ```

- **Runner:** a CLI, `ai-trainer-evals`, runs one template (optionally at a given version or model) and compares against the baseline. Pydantic Evals has no built-in baseline diff (only `report.averages()`, plus comparison in Logfire, which is not adopted), so the runner writes and diffs the baseline JSON itself.
- **Thresholds** live in each baseline file. Default: no drop in the code-graded pass rate, and at most 0.05 drop in any average judge score.
- **When evals run:** on demand, locally or through a GitHub Actions `workflow_dispatch` job. Never in the default CI, because they cost money.
- **Datasets grow** eval-first (cases are written before the prompt change), from bugs, and from draft corrections on the confirm card — the last only from internal or consenting accounts, anonymised.

### Invariants

1. Any change to a prompt template, a model ID or the router comes with an eval run that shows no regression beyond the threshold, and the new baseline is committed in the same change.
2. Eval cases are added before the template change they test.
3. The judge model is explicit, goes through OpenRouter, and differs from the model under test.
4. Evals never run in the default CI; pytest never calls real models.
5. No real user data enters a dataset without consent and anonymisation.

## Consequences

- M0 builds the harness and the CLI.
- The `/tune-prompt` skill runs the eval loop; `.claude/rules/llm.md` carries the invariants.
