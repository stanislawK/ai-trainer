# ADR 0009 — Prompt evaluation pipeline

| | |
|---|---|
| Status | Accepted |
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
- **Judge model:** pinned in settings, different from the model under test, at temperature 0 when its model profile accepts sampling parameters (see Consequences), and set explicitly on every run with `set_default_judge_model(OpenRouterModel(settings.eval_judge_model))`. Otherwise `LLMJudge` falls back to its built-in default (`openai:gpt-5.2`) and bypasses OpenRouter.
- **Layout:**

  ```
  evals/
    datasets/<template_id>.yaml    # committed; Dataset.to_file / Dataset.from_file
    baselines/<template_id>.json   # committed; version, model, averages, per-case scores, thresholds
    reports/                       # gitignored
  ```

- **Runner:** a CLI, `ai-trainer-evals`, runs one template at the version the app runs (or `--version N`, or another `--model`) and compares against the baseline. It writes the baseline only with `--promote` (#208). Pydantic Evals has no built-in baseline diff (only `report.averages()`, plus comparison in Logfire, which is not adopted), so the runner writes and diffs the baseline JSON itself.
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
- Owner-directed amendment, 2026-10-10 ([ADR-0020](0020-decision-models.md)):
  - **Decision sets** are evaluated like templates. Each has `evals/datasets/<decision_id>.yaml`, graded in code against expected labels, and its baseline also records the calibrated thresholds plus each case's probability. A threshold change goes through `/tune-prompt` like a model change (invariant 1).
  - **`JevCheck`** is an auxiliary evaluator that asks Jev a yes/no rubric question about the output. It runs next to `LLMJudge`, which stays the judge of record (invariant 3). Its agreement with the judge is reported, and it gates a baseline only once the owner promotes it at a `/tune-prompt` gate.
- Silent-failure gotcha, found at #109: a case whose task raises (a 401 from a wrong `OPENROUTER_API_KEY`, a timeout) is listed in `report.failures` and drops out of `report.averages()`. A run where every case failed therefore wrote `assertions: 1.0` and exited 0. `run_eval` now raises `CaseFailedError` after writing the report and before saving a baseline, the same way it already raises `EvaluatorFailedError` for a crashed evaluator. The CLI prints either error and exits 1. A blank key never reaches a case: `OpenRouterProvider` raises a bare `UserError` on an empty key, so `main()` checks for a blank key first, prints the fix and exits 1.
- Optional trailing kinds, approved at #127's plan gate, 2026-10-10: a router case may list `metadata.optional_trailing_kinds`. `IntentKindsMatch` ignores those kinds at the end of the output, so a pain case passes with or without the log that follows it. A trailing optional kind never excuses a missing or late `wellbeing_or_injury`. `feeling unwell` and `injury wording in slang` use it for `log_session`. #126's baseline came from a 34-case dataset and #127's from 44, so #127's AC compares v2 against v1 on the same 44 cases. The router baseline's thresholds became 0.05 for assertions and 0.1 for scores.
- Decision-set runs, approved at #120's gates, 2026-10-11:
  - `ai-trainer-evals run <id>` treats an ID with a folder under `src/ai_trainer/llm/decisions/` as a decision set. It runs on `DECISION_MODEL` without a judge model and grades each labelled question in code, a probability at its threshold from `Settings`. An ID that names both a template and a decision set is refused.
  - The baseline adds `decision_thresholds` and `cases`, which hold each case's mean, min, max, spread and run count per question, plus `flips` when the grade or a choice or score answer differed between repeats. A template's baseline file keeps its old shape.
  - A version, model or threshold different from the committed baseline prints a `CHANGED:` line. It is reviewed like a model change and doesn't fail the run; a pass-rate drop beyond the tolerance still does.
- `JevCheck`, approved at #124's gates, 2026-10-11:
  - A check is a `JevCheck` evaluator in the dataset. It names a decision set, a probability question (`rubric`), a `threshold` and optionally `pairs_with`, the `LLMJudge` assertion it is compared with (by default the one named like the rubric). Jev reads the case's message and the reply as JSON.
  - Its result is a Pydantic Evals label, `jev:<rubric>` = `pass`, `fail` or `errored`. Labels never enter `assertions` or `scores`, so a check can't gate or move a baseline. Any failure inside a check, a Jev error or a misconfigured set or rubric, becomes `errored` and the run continues; the dataset's own test catches misconfiguration in CI.
  - The run prints a JevCheck vs LLMJudge table, agreement per rubric and overall, and the errored count. All of it goes into the gitignored report, not the baseline.
  - Agreement is per rubric because only `no_diagnosis` pairs with a judge that asks the same question. `pain_first` pairs with `pain_safety` and `no_free_text_question` with `offers_options`; adding exact judges would have changed chitchat's pass rate. Thresholds are 0.5, uncalibrated, and are calibrated at the `/tune-prompt` gate that promotes a check.
- Judge temperature, owner-directed amendment, 2026-10-11 (#140): a reasoning judge runs at its default, with no `temperature`. Pydantic AI drops sampling parameters from a reasoning model and warns on every request, so asking for `temperature=0` promised a setting the judge never got. The CLI decides from Pydantic AI's model profile, never a list of model IDs: no `temperature` when the profile has `openai_supports_reasoning` or `thinking_always_enabled`, `temperature=0` otherwise. `supports_thinking` is not used, because OpenRouter profiles set it for nearly every ID. A judge ID whose profile says nothing about reasoning keeps `temperature=0`. The judge's grading is unchanged, so no baseline moves.
- `router_fast`'s eval, owner-directed amendment approved at #122's plan gate, 2026-10-11. It is the one decision set without a dataset of its own: it runs on the router's cases (`evals/datasets/router.yaml`, no copy), whose labels already say what a right route is.
  - Each case is asked with that case's `athlete_sports`, routed by the app's own `fast_intents` at the `Settings` thresholds, and a taken case is graded with the router's own matching (`optional_trailing_kinds`, `acceptable_sports`).
  - The assertion `fast_route_safe` passes a declined case and a taken case routed right. The baseline's `scores` are `coverage`, the share of case runs taken, and `accuracy`, the share of taken runs routed right; `decision_thresholds` holds the three thresholds.
  - Plausible sports (#123): a case may list `metadata.plausible_sports`. The assertion `plausible_sports_right` compares the sports Jev leaves in play with it as a set, whether or not the fast path took the case, and the baseline `scores` gain `plausible_accuracy` over the graded runs; `decision_thresholds` gains `plausible_floor`. The router's own evaluators ignore the key.
  - The run refuses a baseline and exits 1 when `accuracy` is below the router baseline's `assertions`, when it took no case, or when there is no router baseline.
- Default version and `--promote`, approved at #208's plan gate, 2026-10-21. A run without `--version` measured v1, not the app's version (seen at #164), every run overwrote the committed baseline, and an older version could not load a dataset holding question cases (seen at #206).
  - Without `--version`, a template or decision set runs at the version the app runs, read from where the app reads it: a plugin's `ExtractionSpec.version`, `ACTIVE_ROUTER_VERSION`, the chitchat template's version, `ROUTER_FAST_VERSION` and `SAFETY_SCREEN_VERSION`. An ID or version with nothing registered exits 1 with one line naming the known versions, before any key is read or model built. The run prints `<id> v<N> (<model>)` before its first model call.
  - Every run writes its report and diffs against the committed baseline. Only `--promote` writes the baseline, for templates and decision sets alike, keeping the committed thresholds. A promote that regresses still writes, prints the drop and exits 1; the owner approves it at the `/tune-prompt` gate.
  - An extraction template whose plugin can ask reads its dataset with the draft and the question type at every version, so a version from before the question loads the question cases and fails their `QuestionMatch`.
- Superseded in part by [ADR-0034](0034-lean-evaluation.md), accepted 2026-10-31: invariants 1 and 2 and the per-change baseline gate. A change passes its smoke set; the datasets, repeats, judge and baselines here stay as the full run, on demand.
