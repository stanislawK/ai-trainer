# ADR 0006 — Training domain and sport extensibility

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (G4, B1, B2, B5, B12, B18, B20), PRD-0003 (B9, B23, F6, F15), ADR-0003, ADR-0004, ADR-0008, ADR-0014, ADR-0016 |

## Context

Three sports ship together in M1, users cross-train, and more sports must be addable without rework (G4). Cross-sport load analysis (B5, B12) needs one vocabulary across very different activities.

## Decision

**Session envelope** (sport-agnostic): `id`, `user_id`, `occurred_on` (plus an optional start time), `duration_min`, `rpe` (1–10, optional), `feeling`, `notes`, `raw_text`, `source` (`chat` | `manual`), `sport`, `payload` (JSONB) and `payload_version`.

**Sport payload:** a Pydantic discriminated union on `sport`. Each payload model lives in its plugin.

**Route identity** (climbing): an activity may name a route — route name, crag, ascent style (`onsight` | `flash` | `redpoint` | `repeat` | `attempt`) and attempt count (B18). Name and crag resolve against the shared reference tables (ADR-0016) when the athlete names one. A session logged without a route name stays valid; nothing in the envelope depends on an external lookup succeeding.

**Sport plugins** in `src/ai_trainer/domain/sports/`:

```
base.py        # SportPlugin protocol, Session envelope, TrainingLoad
registry.py    # SportRegistry — the only list of sports
climbing/  gym/  cycling/
  payload.py   # payload model + version
  normalize.py # scales: Font / French / V / YDS grades; kg, reps, RIR; distance, elevation, zones
  load.py      # load calculator → TrainingLoad
  plugin.py    # wires the above, names its extraction template and eval dataset, KB tags, and its Lucide icon
```

Each plugin declares one Lucide `icon` name (climbing `mountain`, gym `dumbbell`, cycling `bike`). The UI uses it for every mention of the sport: tabs, tables, the draft card and the reply glyph (F15, ADR-0019).

Each plugin also owns an extraction prompt template (`src/ai_trainer/llm/prompts/<sport>.extract/`, ADR-0008) and an eval dataset (`evals/datasets/<sport>.extract.yaml`, ADR-0009).

**Athlete profile** (F6, onboarding in PRD flow 1). What onboarding collects is stored in user-owned tables (ADR-0004, invariant 1):

- `users.timezone` (ADR-0014) and `users.onboarded_at`, a `timestamptz` that stays null until onboarding finishes. An `active` user without it is sent to onboarding.
- `user_sports`: one row per sport the athlete trains. `sport_id` is a registry ID stored as text and validated against `SportRegistry` in the application layer, never a database enum, so a new sport needs no migration (invariant 2).
- `weekly_availability`: one row per weekday the athlete can train, with the minutes available.
- `goals`: free text, an optional `target_date` and an optional `sport_id`. A goal with a sport belongs to one of the athlete's `user_sports`. A goal without one is a general goal, such as "stay consistent" or "lose 5 kg". `sport_id` is a registry ID stored as text, as in `user_sports`. Onboarding shows one card per picked sport plus a *General* card, and needs at least one goal in total. M4 plans (B9) extend this table rather than replace it.

**Common load vocabulary** (`TrainingLoad`, one row per session): session-RPE load = RPE × duration in minutes, plus a strain score for each body system — `aerobic`, `anaerobic`, `max_strength`, `finger_forearm`, `upper_pull`, `upper_push`, `lower_body`, `core`. Each plugin's load calculator computes these; this is how a board session plus a back day is flagged (B12).

**Grades:** stored as the original string, its scale, the canonical French equivalent and an ordinal value. French is the canonical scale in the database (B20); the display scale is a profile preference that defaults to French. Conversion between French, Fontainebleau, V-scale, YDS and UIAA lives in `src/ai_trainer/domain/grades.py` (ADR-0016), not in a plugin, because comparisons cross sports and sources. Case matters in the original: "6A" is Fontainebleau (boulder), "6a" is French (sport).

### Invariants

1. Core code never branches on a sport name; sport-specific behaviour lives only in plugins.
2. Adding a sport means a new plugin, a registry entry, an extraction template, an eval dataset and tests — no core change and no schema migration.
3. `raw_text` is always stored (B2).
4. A payload change bumps `payload_version`; older versions stay readable by upcasting on read.
5. The router's sport list, the payload union, the UI's sport options and the sport glyphs all derive from `SportRegistry`.
6. Route identity is optional, and a grade the athlete typed is never overwritten by a fetched one (ADR-0016).

## Consequences

- The `/add-sport` skill implements invariant 2.
- Phased delivery, owner-directed 2026-09-29, at acceptance:
  - The M1 skeleton ships `registry.py` with each plugin's ID, label and Lucide icon only. That is enough for onboarding's sport picker and the reply glyph.
  - Payloads, normalizers, load calculators and extraction templates arrive with M1 logging. `/add-sport` applies once they exist.
  - The athlete-profile tables are added in the same change.
- M2 may refine the load formulas through a new ADR once real data exists.
- Sport-specific goals, owner-directed 2026-09-30 at #76's acceptance gate:
  - `goals` gains a nullable `sport_id`. #76 shipped goals with no sport; those rows stay as general goals, so the migration needs no backfill.
  - A goal's sport is validated against `SportRegistry` and against the athlete's own `user_sports` in the application layer, never by a database enum (invariant 2).
  - Dropping a sport that has goals, in Settings or by redoing onboarding step 1, opens a dialog. It lists that sport's goals and offers *Keep as general goals* or *Delete them*. Nothing changes until the athlete chooses. Cancelling keeps the sport and its goals (owner-directed 2026-09-30).
  - The Onboarding mock's step 3 in Claude Design already shows one card per sport. It needs a target date and add/remove rows in each card, a *General* card and a desktop frame before the UI is built (ADR-0019).
- Amended by [ADR-0022](0022-training-session-schema.md), accepted 2026-10-12: the session envelope's `raw_text` and `source` move to `training_session_sources`, so a session merged from several logs keeps every original text (B2, B16), and the envelope gains `timezone`. Invariant 3 still holds: `raw_text` is stored on each source row, and every session has at least one (ADR-0022 invariant 2).
- Amended at #164, owner-directed 2026-10-21 and proposed with that ticket's PR: the climb's kind settles a Font or French grade's scale, whatever its letter case. Athletes type the letter in either case, so on a boulder "7a" is Font and on a lead or top-rope climb "7A" is French. The original string is stored as typed, and case still decides when the kind is unknown. V and YDS grades keep their own scale. This refines the Decision's "case matters in the original", and applies to every climbing draft and edit (`domain/sports/climbing/normalize.py`). The plugin also gains two optional hooks on `ExtractionSpec`, used by climbing only (ADR-0015, #164 amendment):
  - `question`: what its extraction template may return instead of a draft;
  - `recent`: its context read from the athlete's latest sessions, such as the climbing kinds they do.
- Amended at #206, owner-directed 2026-10-21 at that ticket's plan gate and proposed with its PR: gym and cycling use the two `ExtractionSpec` hooks too, so climbing is no longer the only one (ADR-0015, #206 amendment). Their `recent` context is computed from payloads in code. For gym it is the exercises of the athlete's latest sessions, each with its load's unit ("bench press (kg)"). For cycling it is the distance units of their latest rides. A unit the log leaves out comes from that history:
  - A gym load typed without a unit takes the unit the log or the athlete's recent messages give. Else it takes that exercise's unit in the recent sessions, else the one unit all the recent loads were in. With no recent loads it is kg, as chosen at #159.
  - A ride's distance typed without a unit follows the same rule over the recent rides, and with none it is km. A bare number is a distance only when the log gives it as one ("rode 60"), not a time, speed or elevation. Before this, such a number was dropped.
  - These defaults are metric because all athletes are in Europe. When the recent history mixes kg and lb, or km and mi, and nothing settles the unit, the extraction asks. The original text stays as typed, and code still does every conversion.
- Europe first, owner-directed 2026-10-25 ([PRD 0008](../prd/0008-ai-training-companion.md) G15). This sets priorities; the Decision and the invariants stand:
  - French and Fontainebleau grades, and the Polish/Kurtyka and UIAA scales as they arrive (ADR-0027, ADR-0016), must be read, stored and converted reliably. V and YDS keep their scale and the support built so far, but are best effort: a V or YDS grade the normalizer can't place keeps its original string with no French equivalent, and such a gap is a later ticket, not a blocker.
  - Metric units (kg, km, m) are the default and the bar. The kg/lb and km/mi handling from #206 stays. lb, mi and ft are best effort, and no new imperial-only reading or question is added without a sign that athletes need it.
- Amended by [ADR-0027](0027-venues-gym-grading-and-device-location.md), accepted 2026-10-25: the climbing payload's `GradeScale` gains `gym` and `kurtyka`, which bumps `CLIMBING_PAYLOAD_VERSION`. A `gym` grade gets a French equivalent and an ordinal only from the athlete's mapping for that venue, and grade conversion skips it.
- Superseded in part by [ADR-0032](0032-per-sport-activity-tables.md), accepted 2026-10-31: invariant 2. A new sport is a plugin, a registry entry, its tables and their migration, an extraction template, a smoke set (ADR-0034) and tests, with no core change.
