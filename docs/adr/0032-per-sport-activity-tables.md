# ADR 0032 — Per-sport activity tables

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-30 |
| Related | PRD-0010 (G4, B4, B5, B6, B37, B38, B39, F12, G13), PRD-0013 (B38, B42–B44), ADR-0004, ADR-0006 (supersedes invariant 2's "no schema migration"), ADR-0022 (supersedes "Inside the payload" for gym), ADR-0024, ADR-0027, ADR-0031 |

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
  - `measure`: text with `CHECK (measure IN ('load_reps','reps','time','distance','band_reps'))`; `'distance_time'` joins it (amended 2026-11-23, see Consequences).
  - `circuit` and `rounds`: smallint NULL, both set or both NULL (amended 2026-11-23).
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
  - `incline_pct`, `machine_level`, `watts`: NULL, as the athlete states them (amended 2026-11-23).
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
6. A pace, a round count or a machine setting is never estimated; pace is computed in code from a set's distance and time and never stored (amended 2026-11-23).

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
  - `gym.extract` still lists one activity per change, so "10, 8, 6" arrives as three. Code merges them after extraction (#410): neighbouring activities with the same name (case folded), side, implements, RIR and measure become one entry whose sets are theirs in order, unless that passes 100 sets. The template and its smoke set are unchanged, and the athlete names a merged exercise once.
  - A set count of one and no set count are the same in v2. Until #398 the card shows "1x5" as "5 reps", and a bare "1 set" shows no scheme.
  - Undoing a log keeps a band it added: a band is the athlete's, not the log's. Its `rank` stays NULL until the athlete places it.
  - `GymPayload` recognises a v1 exercise by its shape, a `name` key, not by `payload_version`: a reader of a draft has no version to hand, and a log added to a session moves that column while the JSON stays v1. An undo of a pre-tables log writes the old payload back in the v2 shape.
- Implementation details of gym payload v2 (#395), for the owner's review of its draft PR:
  - The model never names the `measure`. Code reads it from the set: time, then distance, then band, then load, else reps. Time and distance arrive as a value and a unit, and code converts them, as it does kg. A time in parts or hours is the one exception: the model gives its total.
  - A set count is capped at 100, because each set is a row. A set's time is capped at a day, and its distance at what `gym_sets.distance_m` holds. A v1 draft past 100 sets no longer reads.
- Owner-approved 2026-10-31, at #398's plan gate, settling what #395 left to it:
  - #398 does not merge a pyramid's entries at extraction; #410 does, in code, so no template change is needed. The draft card, the logged card and the edit form show and edit whatever v2 holds, set by set: an entry with several sets reads as one line ("10 @ 60 kg · 8 @ 70 kg · 6 @ 80 kg"), and pyramids drafted before #410 read as consecutive blocks.
  - The edit form posts every field of every set, the ones the measure doesn't use hidden. A field it leaves out stays as stored and a blank one is saved as NULL, never 0. Time is entered in seconds and distance in metres.
  - The edit derives `measure` in code from the first set that holds a value, as an extraction does, so it follows a load the athlete cleared. The athlete's pick on the measure select counts only while every set is blank.
- Proposed 2026-11-02 for the owner's review with #431's draft PR (the plan gate approved it), settling what #400 left to it:
  - The athlete's first band takes `rank` 1 in the confirm's transaction, with no question. Every later band is stored with `rank` NULL and asked about once, on a choice card (ADR-0015, #431 amendment). The card's answer renumbers `rank` for the athlete's bands only, in one transaction that locks them: the bands from the picked rank on move one up. Nothing in `athlete_bands` is ever ranked from its colour.
  - A band question lives in `clarifications` (`purpose` `band`), so no migration is needed and a skipped band stays NULL. `GymActivityStore.ask` and `SqlAlchemyBandOrder` in `adapters/gym_band_order.py` are the only code that reads or ranks bands for it.
- Proposed 2026-11-08 at #516's plan gate (owner-chosen), for the owner's review with the draft PR:
  - The athlete's edit of a saved session from History goes through `ActivityStorePort.replace`, in one transaction that locks the session's row (`adapters/training_session_editor.py`) and sets `updated_at` from the clock. A payload sport keeps the edited payload. The gym store updates each entry's measure, each side, implements and RIR in place, keeping its `id`, `source_id`, `exercise_id` and `name_as_typed`, and replaces its sets. An edit whose exercise count no longer matches the rows is refused (409) and writes nothing. History never renames an exercise; renaming stays with the chat's exercise card (B37).
  - A band first named in a History edit joins `athlete_bands` with `rank` NULL through the store's `_band_ids`, as a skipped band does. History asks no band card; the chat's next band question asks about it.
- Proposed 2026-11-08 at #518's plan gate (owner-approved), for the owner's review with the draft PR:
  - History's delete is ADR-0022's hard delete: one `DELETE` of the `training_sessions` row filtered by `id` and `user_id` (`SqlAlchemySessionDeleter` in `adapters/training_session_editor.py`), with no `ActivityStore` call. Its sources, `gym_exercise_entries` and `gym_sets` go by their existing cascades; the drafts that made it keep their row with `session_id` NULL. The athlete's bands, aliases and custom exercises stay (ADR-0031 invariant 3). An unknown session, another user's or one already deleted deletes nothing and is a 404. No migration.
- Owner-directed 2026-11-23 (#618), for the owner's review with the draft PR: cardio, circuits and machine settings ([PRD 0013](../prd/0013-ai-training-companion.md) B38, B42–B44). Built by M2f's tickets once PRD 0013 is Approved; nothing is built yet.
  - **`measure`** gains `'distance_time'`: a set holding both `distance_m` and `duration_s`. Code's derivation becomes distance and time → `distance_time`, then time, distance, band, load, else reps. Machine settings alone hold no measure: a set with only incline, level or watts counts as blank there, so the edit form's rule (the athlete's measure pick counts while every set is blank, #398) still applies.
  - **Pace** is never stored. Code computes it as `duration_s / (distance_m / 1000)` seconds per km from a set holding both, and lower is better when progress is read (B40, B42). A set with only one of the two, or with either at 0, has no pace.
  - **Machine columns** on `gym_sets`, all NULL and set only when the athlete states them: `incline_pct numeric(4,1)` (`CHECK BETWEEN -10 AND 40`), `machine_level smallint` (`CHECK BETWEEN 1 AND 50`), `watts smallint` (`CHECK BETWEEN 1 AND 2000`).
  - **Circuits** on `gym_exercise_entries`: `circuit smallint NULL` (a group number within the session; entries with the same number form one circuit) and `rounds smallint NULL` (`CHECK BETWEEN 1 AND 100`), with `CHECK ((circuit IS NULL) = (rounds IS NULL))`. Each round is a set (`set_no` is the round). No new table, so G7's coverage is unchanged.
  - **Migration:** expand-only (ADR-0024), nullable columns. The `measure` CHECK is replaced by adding the wider one `NOT VALID`, validating it, then dropping the old one, as squawk requires.
  - **Invariants** 4 and 5 hold. Invariant 6 (new): a pace, a round count or a machine setting is never estimated, and pace is computed in code only.
- Owner-directed 2026-11-23 at [#622](https://github.com/stanislawK/ai-trainer/issues/622)'s plan gate, for the owner's review with the draft PR: stretching, classes and timed work ([PRD 0013](../prd/0013-ai-training-companion.md) B41). No migration.
  - **Effort.** `gym.extract` (v6) picks a closed effort word (`very_easy`, `easy`, `moderate`, `hard`, `very_hard`, `max`) or copies a stated number ("RPE 8", "8/10"). Code reads the word as the draft's `rpe` through one table in `domain/sports/gym/extraction.py`: 2, 3, 5, 7, 9, 10. A stated number wins over a word. With neither, `rpe` stays NULL (invariant 4).
  - **Class time.** A class is one entry named as typed ("yoga class") with one timed set (60 min is 3600 s), so a mixed log keeps it. The draft's `duration_min` is set only when the class is the whole log or the log states the whole session's length ("1 h session"), rounded up to whole minutes in code.
  - The extraction reports both through `SessionExtraction.envelope()`, empty for a sport that reports neither, so no core code names the sport. A set's time may also come in hours, converted in code.
- Owner-approved 2026-11-24 at [#626](https://github.com/stanislawK/ai-trainer/issues/626)'s plan gate, built by #626, for the owner's review with the draft PR (the #618 amendment's `distance_time` and pace):
  - **Pace unit.** Per 500 m when the canonical or typed name, case-folded, holds one of the whole words `row`, `rower`, `rowing`, `erg` or `ski erg`, a word starting `ergomet`, or a word starting `wiosł` or `wiośl` (a rowing machine or ski erg; `wiośl` covers "wioślarz"; whole words, so "throw" or "arrow" never count, settled in the build and its code review), otherwise per km. A lift such as "barbell row" may match, but it holds no distance with a time, so it never shows a pace. On a per-km split a distance from 1000 m reads in km ("5 km in 25:00"); on a 500 m split it stays in metres. Progress plots speed in km/h (3600 / seconds per km), so faster plots higher, and its table keeps the pace. `domain/sports/gym/pace.py` holds it; the "/500 m" and "/km" text is the templates'.
  - **Precision.** A pace and a time read as a clock, "7:50" or "1:02:30", with tenths only when they aren't zero ("1:57.5").
  - **Migration.** Three revisions, expand only: `3c7e1a9f5d24` adds `gym_exercise_entries_measure_v2` and `exercises_default_measure_v2` with `'distance_time'` `NOT VALID`; `8b2f6d4e1a73` validates them; `5e9a2c7b4f18` drops the narrower CHECKs and moves each entry measured by `time` or `distance` whose first set holding any value (lowest `set_no`) has both a distance and a time to `distance_time`, the rule `measure_of` and the edit read a set by. Its sets are untouched, and the move is declared for the upgrade-with-data check (ADR-0024 #626 amendment). `exercises.default_measure` is widened too, since a custom exercise takes its entry's measure (ADR-0031). Once a `distance_time` row exists, release N-1 can't read it, so an app rollback past this release is fixed forward (owner-accepted).
  - **Payload v6.** `GYM_PAYLOAD_VERSION` is 6. A v5 draft is upcast on read: a `time` or `distance` exercise whose first given set holds both values reads as `distance_time`, as the migration moves saved entries. An athlete's explicit `time` pick on such a set moves too, since the measure follows the sets (#398).
