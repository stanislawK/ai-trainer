# ADR 0032 — Per-sport activity tables

| | |
|---|---|
| Status | Accepted |
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

**A sport owns its tables.** A plugin may define tables named `<sport>_*`, created by its own migration. The confirm and undo transactions (ADR-0022) reach them through the sport's `ActivityStore`: a port in `application/ports/` with implementations in `adapters/`, mapped by `sport_id` in the composition root, because `domain/` can't import an adapter. Climbing and cycling share one default store over today's `extend_payload` and `withdraw_payload`. Core code still never branches on a sport name (ADR-0006 invariant 1). `training_sessions` keeps the envelope. The ORM models of a sport's tables live in `adapters/orm/sports/<sport>.py`, imported by `adapters/orm/__init__.py` (placement row in ADR-0003, "Where code lives"; split in #345).

**Gym** is the first sport to move. Every table is user-owned: `user_id` NOT NULL, FK `users` CASCADE, indexed, and each reference keyed with `user_id` (ADR-0022, 2026-10-13 amendment).

- **`gym_exercise_entries`**: one row per exercise in a session.
  - `session_id`: FK `training_sessions` CASCADE.
  - `source_id`: nullable FK `training_session_sources` CASCADE: the log that brought this entry. Undo deletes that log's source row, so a log added to a session (B16) takes only its own entries with it. It is null only on backfilled rows.
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

**Drafts stay JSONB.** `session_drafts.draft` keeps the validated draft. Gym payload v2 carries `exercise_id`, `name_as_typed`, `measure`, `per_side`, `implements` and a list of sets. Confirming writes typed rows in the same transaction as the session (ADR-0022 invariant 3). Undoing the session's only log deletes them through the session's cascade, and undoing a later log through `source_id`.

**Migration, one cut-over.** Owner-directed 2026-10-31. Production holds no athlete data and no release comes before M1g (PRD 0010), so G13 doesn't bind yet and no running app needs the old payload. ADR-0024's expand/contract would only add dual-write bugs:
- Create the three tables.
- Gym confirms write rows only, and gym sessions store `{}` as payload. Readers (history, MCP tools, stats, the extraction's recent exercises) read the rows.
- One idempotent backfill reads each existing gym payload through its upcaster and writes typed rows with `exercise_id` and `source_id` NULL. It leaves the JSON alone (ADR-0022 invariant 4). Undoing a log added before the backfill removes the matching entries by name as typed, latest first, as `withdraw_payload` does.

If athlete gym data reaches production before this ships, stop and amend this ADR: the move is then an expand/contract under ADR-0024.

**Climbing and cycling** keep their payload as the record until a ticket moves each, with the same pattern.

### Invariants

1. A confirmed gym session's activities are read from `gym_exercise_entries` and `gym_sets`, never from `payload`, once the tables ticket has shipped.
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
- Owner-directed 2026-10-31, at #376's plan gate. Until gym payload v2, a v1 draft is read into rows like this:
  - An exercise with no set count is one set row with the reps and load the athlete gave. Rows can't say "an unknown number of sets", and none would drop what was given.
  - `measure` is `load_reps` when a load was given, otherwise `reps`.
  - The recent exercises' unit hint (#206) is read back from the rows: `kg` when the first typed number in `load_original` equals `load_kg`, `lb` when it converts to it, otherwise no hint. No `load_unit` column.
  - `gym_exercise_entries.exercise_id` has no `ON DELETE` action. An account deletion removes the athlete's entries and custom exercises in one statement, which the G7 test covers.
  - A gym log added to a session confirmed before the tables writes its rows from position 1 and leaves the old payload as it is. #377's backfill adds the earlier activities.
  - The `ActivityStorePort` is generic over the transaction, so `application/` never names SQLAlchemy. `UNIQUE (id, user_id)` on `training_session_sources` is built concurrently and attached with `USING INDEX`.
- Owner-approved 2026-10-31, at #395's plan gate:
  - A pyramid stays one entry per scheme for now: `gym.extract` still lists one activity per change, so "10, 8, 6" is three entries. The v2 shape can hold it as one, and #398 settles merging them.
  - A set count of one and no set count are the same in v2. Until #398 the card shows "1x5" as "5 reps", and a bare "1 set" shows no scheme.
  - Undoing a log keeps a band it added: a band is the athlete's, not the log's. Its `rank` stays NULL until the athlete places it.
  - `GymPayload` recognises a v1 exercise by its shape, a `name` key, not by `payload_version`: a reader of a draft has no version to hand, and a log added to a session moves that column while the JSON stays v1. An undo of a pre-tables log writes the old payload back in the v2 shape.
- Implementation details of gym payload v2 (#395), for the owner's review of its draft PR:
  - The model never names the `measure`. Code reads it from the set: time, then distance, then band, then load, else reps. Time and distance arrive as a value and a unit, and code converts them, as it does kg. A time in parts or hours is the one exception: the model gives its total.
  - A set count is capped at 100, because each set is a row. A set's time is capped at a day, and its distance at what `gym_sets.distance_m` holds. A v1 draft past 100 sets no longer reads.
