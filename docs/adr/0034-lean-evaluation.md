# ADR 0034 — Lean evaluation

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-30 |
| Related | PRD-0010 (G3, G6, G15), ADR-0008, ADR-0009 (supersedes invariants 1–2 and the per-change baseline gate), ADR-0013, ADR-0018, ADR-0020 (supersedes invariant 6's calibrated-before-active rule), ADR-0026 |

## Context

ADR-0009 gates every prompt, model, router or threshold change on a full eval run:
- cases written first;
- datasets of 20–45 mostly synthetic cases;
- repeats, an LLM judge, and a committed baseline with no regression.

ADR-0020 adds a calibrated baseline before any Jev set goes live.

The owner's verdict on 2026-10-30: this is too slow and too costly for how much is left to build, and the synthetic cases can be far from real use. The first real gym session failed in ways no dataset case covered: a band colour, "each side", an invented "4 × 10".

We still need to check that a change does what was intended. But the check should be fast and cheap, start from real chats, and grow from athletes' feedback instead of from guessed edge cases.

## Decision

**Smoke sets are the gate.**
- **What one is:** each template and each decision set has `evals/smoke/<id>.yaml`, about 5–10 cases.
- **Where cases come from:**
  - mostly cut from [field transcripts](../../evals/field/README.md), each case's `metadata.field` naming its transcript;
  - synthetic cases only for a reported bug or a safety rule;
  - until a template has a transcript behind it, about 5 cases taken from its existing dataset, replaced as real chats arrive (owner-directed 2026-10-31, to spend as little as possible on them).
- **Grading:** in code only, with the existing evaluators: schema validity, the fields that matter, and no value the message didn't give.
- **Runs:** one per case, with no repeats and no judge. `uv run ai-trainer-evals smoke <id>` runs one set, and `smoke --all` runs every set.
- **Passing:** every case passes, or is listed under `known_gaps` with a ticket number.

**What a change needs.** A change to a template, a model ID, provider routing, the router or a threshold:
1. adds or updates its smoke cases in the same change;
2. passes its smoke set;
3. shows the run's output at the ticket's review gate.

There is no eval-first ordering, no separate comparison gate and no baseline promote per change.

**Full runs.** The existing datasets, with repeats, `LLMJudge` and baselines, stay as the full suite. They run on demand only: before a model swap, before a release the owner wants checked, or when the owner asks. `--promote` still writes a baseline, as a reference rather than a gate.

**Safety stays strict.** The `wellbeing_or_injury` and safety-screen cases (G3) are a smoke set that always runs with its judge and must pass. This is the one place a judge is mandatory per change.

**Jev thresholds.**
- **New sets:** a new decision set starts at a conservative threshold, so it abstains more and asks on a card more often.
- **Its smoke set:** at least 5 labelled cases.
- **Tuning:** thresholds move later from feedback (below), recorded in `Settings` with the evidence in the PR. Calibration on a large dataset is no longer needed before a set goes live.

**Feedback events.** `feedback_events` is a user-owned table (`user_id` CASCADE) holding one content-free row per signal:
- **Columns:** `kind`, `template_id`, `template_version`, `fields` (`text[]`), `created_at`.
- **Kinds:** `draft_edited`, `draft_discarded`, `session_undone`, `choice_answered`, `decision_overridden`. `decision_overridden` means the athlete picked something other than Jev's choice.
- **No text:** no message text and no values.

**Review loop.** A monthly owner review reads the counts per template and field (`uv run ai-trainer feedback-report`), picks the patterns, and turns the matching chats into field transcripts and smoke cases. These come from the owner's or consenting accounts, anonymised (ADR-0009 invariant 5).

### Invariants

1. A template, model, routing, router or threshold change passes its smoke set, and the safety set when it touches routing or replies, in the same change.
2. Smoke cases are graded in code, run once, and need no judge, except the safety set.
3. pytest never calls a real model, and evals never run in the default CI (ADR-0009 invariant 4 stands).
4. The judge, when used, is explicit, goes through OpenRouter and differs from the model under test (ADR-0009 invariant 3 stands).
5. No real user data enters a transcript or a case without consent and anonymisation (ADR-0009 invariant 5 stands). `feedback_events` holds no message text or values.

## Alternatives considered

- **Keep ADR-0009 and just trim the datasets.** A promote per change and a judge per run would still cost the most time.
- **No evals until there are users.** Then nothing would show that a prompt still does what was intended, and the invented-reps class of bug would ship silently.

## Consequences

- `/tune-prompt` becomes the smoke loop: cases, change, run, show. The full-run steps move to a "Full run" section. `/add-decision` drops calibration before activation. `.claude/rules/llm.md` and `CLAUDE.md` follow.
- ADR-0026 still picks models on measured latency, cost and evals. A model swap is the trigger for a full run.
- The CLI gains `smoke` and `feedback-report`. The existing `run` and `--promote` stay as they are.
- Owner-directed 2026-10-31, at #394, how `feedback_events` rows are written:
  - `draft_edited.fields` holds the names of the fields the edit changed (`reps`, `rpe`), found by comparing the draft before and after, never from typed text. A payload field is named by its key, one list level deep, so a gym row's `reps` is `reps`. An edit that changes nothing writes no row.
  - `draft_discarded` and `session_undone` write one row on the transition only, so a repeated request writes none. Their `template_id` and `template_version` are the draft's own ([ADR-0022](0022-training-session-schema.md)), NULL for a draft made before drafts kept them.
  - `choice_answered.fields` is the question's purpose (`sport`, `reading`, `same_session`), never the option picked. Its template is the extraction the answer produced, or the draft's own for a same-session answer.
  - Recording is best effort: a failed write is logged by its error type and never fails the athlete's action.
  - `feedback-report` reads every user's rows without who made them. The `kind` check admits `decision_overridden` now so adding it later is no contract step; nothing writes it until the Jev pick.
- G6 in PRD 0010 is reworded to match. The "eval dataset and committed baseline per capability" acceptance criterion becomes a smoke set per capability.
- ADR-0009 invariants 1–2 and ADR-0020 invariant 6 are marked Superseded-in-part once this ADR is Accepted.
