# ADR 0032 — Per-sport activity tables

| | |
|---|---|
| Status | Proposed |
| Date | 2026-10-30 |
| Related | PRD-0010 (G4, B4, B5, B6, B37, B38, B39, F12, G13), ADR-0004, ADR-0006 (supersedes invariant 2's "no schema migration"), ADR-0022 (supersedes "Inside the payload" for gym), ADR-0024, ADR-0027, ADR-0031 |

## Context

ADR-0022 keeps a session's activities in `training_sessions.payload` (JSONB), so a new sport needs no migration (old G4).

The owner's first gym session showed what that costs:
- The gym payload holds only name, sets, reps, load and RIR. A plank's time, "each side", "two dumbbells, 6 kg each" and a band's colour had nowhere to go, and were dropped or squeezed into the name.
- Progress per exercise, the point of logging (B6, F12), needs JSON path queries over every session.

The owner chose typed tables per sport on 2026-10-29, accepting a migration per new sport. PRD 0010 revises G4 to match.

Production holds no athlete data yet (the first deploy follows M1g, PRD 0010), so this is the cheapest moment to move.

## Decision

**A sport owns its tables.** A plugin may define tables named `<sport>_*`, created by its own migration. The confirm transaction (ADR-0022) writes them through the plugin's `ActivityStore`, a port the registry resolves by `sport_id`. Core code still never branches on a sport name (ADR-0006 invariant 1). `training_sessions` keeps the envelope. The ORM models of a sport's tables live in `adapters/orm/sports/<sport>.py`, imported by `adapters/orm/__init__.py` (placement row in ADR-0003, "Where code lives"; split in #345).

**Gym** is the first sport to move. Every table is user-owned: `user_id` NOT NULL, FK `users` CASCADE, indexed, and each reference keyed with `user_id` (ADR-0022, 2026-10-13 amendment).

- **`gym_exercise_entries`**: one row per exercise in a session.
  - `session_id`: FK `training_sessions` CASCADE.
  - `position`: smallint, unique with `session_id`.
  - `exercise_id`: nullable FK `exercises` (ADR-0031). It is null only while the entry is typed-only.
  - `name_as_typed`: text NOT NULL.
  - `measure`: text with `CHECK (measure IN ('load_reps','reps','time','distance','band_reps'))`.
  - `per_side`: boolean NOT NULL default false. "12 each side" means 12 reps per side.
  - `implements`: smallint NULL, `CHECK (implements BETWEEN 1 AND 4)`. "Two dumbbells, 6 kg each" means 2, and `load_kg` is per implement.
  - `rir`: smallint NULL, 0–10.
  - `note`: text NULL, ≤ 500 characters.
  - Index `(user_id, exercise_id, session_id)` serves progress per exercise.
- **`gym_sets`**: one row per set, so a pyramid ("10, 8, 6") is three rows and "3 × 8" is three equal rows.
  - `entry_id`: FK CASCADE.
  - `set_no`: smallint, unique with `entry_id`.
  - `reps`: smallint NULL.
  - `load_kg`: `numeric(6,2)` NULL, together with `load_original` (text as typed). Code converts; lb stays best effort (G15).
  - `duration_s`: integer NULL.
  - `distance_m`: `numeric(8,1)` NULL.
  - `band_id`: NULL, FK `athlete_bands` `ON DELETE SET NULL (band_id)`.
  - `band_label`: text NULL, as typed.
  - A set with every measure empty is allowed: "4 sets", reps unknown. Missing values are never invented (B38).
- **`athlete_bands`**: the athlete's resistance bands (B39).
  - `label` and `label_norm`, unique with `user_id`.
  - `rank`: smallint NULL, where 1 is the lightest.
  - `nominal_kg`: `numeric(5,1)` NULL, only when the athlete states it.
  - A colour is never ranked or converted by the app on its own, the same rule as gym grades in ADR-0027. While `rank` is null the band is stored and shown by label, and the athlete is asked to order it once, on a choice card.

**Drafts stay JSONB.** `session_drafts.draft` keeps the validated draft. Gym payload v2 carries `exercise_id`, `name_as_typed`, `measure`, `per_side`, `implements` and a list of sets. Confirming writes typed rows in the same transaction as the session (ADR-0022 invariant 3). Undo deletes them through the session's cascade.

**Migration, expand then contract (ADR-0024).**
1. **Expand.**
   - Create the three tables.
   - An idempotent backfill reads each gym payload through its upcaster and writes typed rows with `exercise_id` NULL. It never rewrites the JSON (ADR-0022 invariant 4).
   - Gym confirms write both the rows and the payload, so a rollback of the app still reads.
   - Readers (the logged card, history, MCP tools, stats) move to the rows.
2. **Contract**, its own ticket after the release that stopped reading gym payloads: gym sessions store `{}` as payload and the old payloads are cleared.

**Climbing and cycling** keep their payload as the record until a ticket moves each, with the same pattern.

### Invariants

1. A confirmed gym session's activities are read from `gym_exercise_entries` and `gym_sets`, never from `payload`, once the expand ticket has shipped.
2. Every gym table is user-owned, keyed with `user_id`, and emptied by account deletion (G7). The tenancy and G7 tests cover each one.
3. Only the athlete's confirm or edit writes these rows (ADR-0022 invariant 3).
4. A measure the athlete didn't give is stored as NULL, never estimated.
5. A sport's tables are created and changed only by migrations that ship with that sport's tickets. Core tables never gain sport-specific columns.

## Alternatives considered

- **Keep JSONB and add fields to the payload** (ADR-0022). This is the least change, but progress queries stay in JSON paths and the shape is unchecked by the database. The owner chose tables (2026-10-29).
- **One `session_activities` table for every sport, with typed metric columns.** It keeps old G4, but mixes grades, rides and sets in one wide, mostly-null row. The owner preferred a table per sport.
- **One row per exercise with `sets × reps`.** It can't hold a pyramid or a set-by-set log, and progress per set (best set, volume) needs the rows anyway.

## Consequences

- ADR-0006 invariant 2 becomes: a new sport is a plugin, a registry entry, its tables and their migration, an extraction template, a smoke set (ADR-0034) and tests, with no core change. ADR-0022's "Inside the payload" no longer applies to gym. Both are marked Superseded-in-part once this ADR is Accepted.
- `/add-sport` gains a "tables and `ActivityStore`" step. `.claude/rules/migrations.md` notes that a sport's tables follow the user-owned table rules.
- The ADR-0022 ⚠ about JSON path latency for M2 stats no longer applies to gym.
- The F2 edit form and the logged card read and write the typed shape. `logged_gym.html` shows the load and the measure, which it drops today.
- `TrainingLoad` (ADR-0006) is computed from the rows. M2 decides whether to cache it.
