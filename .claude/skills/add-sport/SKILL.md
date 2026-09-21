---
name: add-sport
description: Checklist for adding a new sport as a plugin — payload, normalizers, load calculator, extraction template and eval dataset — without touching core code.
argument-hint: "[sport-key]"
---

# Add sport: $ARGUMENTS (ADR-0006)

Precondition: the current PRD includes this sport. If it doesn't, stop and propose a PRD revision with `/update-docs`.

1. Create `src/ai_trainer/domain/sports/<sport>/` with `payload.py` (payload model + version), `normalize.py` (units and scales), `load.py` (load calculator emitting `TrainingLoad` with the common system vocabulary) and `plugin.py`.
2. Register the plugin in `src/ai_trainer/domain/sports/registry.py` — the only change allowed outside the plugin folder, prompts, evals and tests.
3. Write the eval dataset first: `evals/datasets/<sport>.extract.yaml` with at least 20 cases, covering ambiguous input, several activities in one message, a mixed-sport message, unusual units, and a relative date such as "yesterday" or "Tuesday" (ADR-0014).
4. Add the extraction template `src/ai_trainer/llm/prompts/<sport>.extract/v1.en.md` and create its baseline with `/tune-prompt <sport>.extract`.
5. Tests: payload validation, every normalizer, the load calculator, and registry wiring.
6. Prove core is untouched: `git diff --stat main...HEAD` shows changes only in the plugin folder, the registry, prompts, evals and tests.
