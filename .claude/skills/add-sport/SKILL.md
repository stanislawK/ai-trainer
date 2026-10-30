---
name: add-sport
description: Checklist for adding a new sport as a plugin — payload, normalizers, load calculator, its own activity tables, extraction template and smoke set — without touching core code.
argument-hint: "[sport-key]"
---

# Add sport: $ARGUMENTS (ADR-0006, ADR-0032, ADR-0034)

Precondition: the current PRD includes this sport. If it doesn't, stop and propose a PRD revision with `/update-docs`.

1. Create `src/ai_trainer/domain/sports/<sport>/` with `payload.py` (payload model + version), `normalize.py` (units and scales: metric and European first, imperial and American best effort, PRD 0008 G15), `load.py` (load calculator emitting `TrainingLoad` with the common system vocabulary) and `plugin.py`.
   - `edit.py`: the athlete's edit of a draft's payload, named as `apply_edit` on the plugin's `ExtractionSpec`. It applies the edit form's sport fields to the stored payload, keeps what the form leaves out and validates the result with the payload model (F2, ticket #158). Its form fields go in `web/templates/partials/chat/edit_<sport>.html`, next to the card's `draft_<sport>.html` and the confirmed row's `logged_<sport>.html`.
   - **Fan out, then reconcile.** Before writing the files, write a short shared spec: the sport's payload fields, units/scales, and the load formula's inputs/outputs. Draft the four files with one agent per file, in parallel, each given only the shared spec. Then run one synthesis pass that reads all four files plus the spec and confirms field names, units and types agree across them before moving on — fix mismatches here, not in step 7's tests. The tables in step 2 come from the same spec.
2. **Tables and `ActivityStore`** (ADR-0032). Design the sport's `<sport>_*` tables from the shared spec: one row per activity, and one per set or attempt where the sport repeats. Each table is user-owned (`user_id` CASCADE, references keyed with `user_id`), and a measure the athlete didn't give is NULL. Write the migration (`.claude/rules/migrations.md`) and the sport's `ActivityStore` adapter, which the confirm transaction calls. The draft stays JSONB in `session_drafts`.
3. Register the plugin and its `ActivityStore` in `src/ai_trainer/domain/sports/registry.py`. This is the only core change allowed; everything else stays in the plugin folder, its tables' migration and adapter, prompts, evals and tests.
4. Write the smoke set `evals/smoke/<sport>.extract.yaml` (ADR-0034), about 5–10 cases. Take them from `evals/field/<sport>/` transcripts where any exist. Otherwise cover several activities in one message, a mixed-sport message, a European unit or scale in its local form (a decimal comma, Polish words), a relative date such as "yesterday" (ADR-0014), and a message that leaves a value out.
5. Add the extraction template `src/ai_trainer/llm/prompts/<sport>.extract/v1.en.md` and pass its smoke set with `/tune-prompt <sport>.extract`.
6. Decision sets: the sport's options in Jev decision sets (for example its description in `router_fast`'s sport question) gain an entry. Extend those sets' datasets with cases for the new sport and re-run `/tune-prompt <decision-id>`. If the plugin needs a pick, such as mapping an exercise name onto its catalogue, add it with `/add-decision` rather than an LLM template (ADR-0020).
7. Tests: payload validation, every normalizer, the load calculator, the `ActivityStore` (tenancy and account deletion included), the migration's upgrade-with-data test, and registry wiring.
8. Prove core is untouched: `git diff --stat main...HEAD` shows changes only in the plugin folder, the registry, the sport's migration and `ActivityStore` adapter, its three `partials/chat/*_<sport>.html` partials, prompts, decision sets, evals and tests.
