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
- `goals`: free text and an optional `target_date`. M4 plans (B9) extend this table rather than replace it.

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
