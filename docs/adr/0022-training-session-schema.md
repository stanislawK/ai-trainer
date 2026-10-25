# ADR 0022 — Training-session schema

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-11 |
| Related | PRD-0004 (B1–B4, B14–B17, B20, B23, B26, F2, F9, G1, G4, G7, G13), ADR-0004, ADR-0006, ADR-0014, ADR-0015, ADR-0016, ADR-0021, ADR-0024 |

## Context

ADR-0006 fixes the session envelope and sport payloads, but not the tables. Its Consequences say the tables "arrive with M1 logging". These are the first tables to hold the athlete's training history, which is the data the PRD says must never be lost (G13). From the first production deploy, any schema mistake has to be fixed with a data-preserving migration (ADR-0024). So the table design is reviewed here, before the first migration, and not inside a ticket.

The schema also has to fit what the logging flow does:

- **Drafts.** Nothing is saved until the athlete confirms (F2), and a draft must survive a reload.
- **Clarifications.** A clarification suspends a draft or an intent until an option is picked (ADR-0015).
- **Session continuity.** Logs close together join one session, and each keeps its own original text (B2, B16).
- **Several sports per message.** One message can create sessions in several sports (B1, B14).
- **Retention.** Conversations are purged after 14 days (B26, ADR-0021), and saved training data is not.
- **New sports.** A new sport needs no migration (G4, ADR-0006 invariant 2).

## Decision

Four user-owned tables. Each has `user_id` NOT NULL, FK `users` `ON DELETE CASCADE`, and an index (ADR-0004 invariant 1). IDs are UUIDs, as in every existing table.

**`training_sessions`** holds one row per session the athlete confirmed. This is the durable record.

| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid | FK `users` CASCADE |
| `sport_id` | text NOT NULL | a `SportRegistry` ID, checked in the application layer, never a DB enum (ADR-0006) |
| `occurred_on` | date NOT NULL | the athlete's local calendar day. A calendar date, not a moment, as with `goals.target_date` (ADR-0014 invariant 2 covers timestamps) |
| `timezone` | text NOT NULL | the IANA zone `occurred_on` was resolved in, so a later timezone change can't reinterpret it |
| `started_at` | timestamptz NULL | only when the athlete stated a time |
| `duration_min` | integer NULL | `CHECK (duration_min BETWEEN 1 AND 1440)` |
| `rpe` | smallint NULL | `CHECK (rpe BETWEEN 1 AND 10)` |
| `feeling` | text NULL | `CHECK (char_length(feeling) <= 200)` |
| `notes` | text NULL | `CHECK (char_length(notes) <= 2000)` |
| `payload` | jsonb NOT NULL | the sport payload with its activities (ADR-0006). `CHECK (jsonb_typeof(payload) = 'object')` |
| `payload_version` | smallint NOT NULL | `CHECK (payload_version >= 1)` |
| `created_at`, `updated_at` | timestamptz NOT NULL | set from the `Clock` port |

Indexes: `(user_id, occurred_on DESC)` serves history, reports and stats in M2. `(user_id, sport_id, occurred_on DESC)` serves continuity (B16) and recent-session context for sport inference (B23).

**`training_session_sources`** holds one row per log that created or extended a session. It is the B2 record.

| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid | FK `users` CASCADE |
| `session_id` | uuid NOT NULL | FK `training_sessions` CASCADE, indexed with `logged_at` |
| `raw_text` | text NOT NULL | the athlete's original text, copied, never a reference to a chat row |
| `source` | text NOT NULL | `CHECK (source IN ('chat', 'manual'))` |
| `logged_at` | timestamptz NOT NULL | when the log was sent. B16 measures the merge window from the session's latest `logged_at` |

The envelope's single `raw_text` and `source` from ADR-0006 become this child table. Three logs merged into one session (B16) keep three original texts. A message that logged two sports gives one source row to each session.

**`session_drafts`** holds what the athlete hasn't confirmed yet. It is transient and scoped to a conversation.

| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid | FK `users` CASCADE |
| `conversation_id` | uuid NOT NULL | FK `conversations` CASCADE (ADR-0021) |
| `message_id` | uuid NOT NULL | FK `chat_messages` CASCADE, the log it came from |
| `sport_id` | text NOT NULL | |
| `draft` | jsonb NOT NULL | the validated `SessionDraft`: envelope fields, `date_source` (ADR-0014) and payload |
| `payload_version` | smallint NOT NULL | |
| `extends_session_id` | uuid NULL | FK `training_sessions` `ON DELETE SET NULL`, the session B16 offers to add this to |
| `status` | text NOT NULL | `CHECK (status IN ('pending', 'confirmed', 'discarded'))` |
| `session_id` | uuid NULL | FK `training_sessions` `ON DELETE SET NULL`, the session a confirm produced |
| `created_at`, `resolved_at` | timestamptz | `resolved_at` is null while the draft is pending |

Index: `(user_id, conversation_id, status)`.

A confirm happens in one transaction: it writes the session (or extends `extends_session_id`), adds its source row, and sets the draft to `confirmed`. That transition happens once. Confirming a draft twice is a no-op that returns the same session.

**`clarifications`** holds the open questions (ADR-0015).

| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid | FK `users` CASCADE |
| `conversation_id` | uuid NOT NULL | FK `conversations` CASCADE |
| `message_id` | uuid NOT NULL | FK `chat_messages` CASCADE |
| `draft_id` | uuid NULL | FK `session_drafts` CASCADE, set when the question blocks a draft |
| `purpose` | text NOT NULL | what is being asked, e.g. `sport`, `same_session`, `intent`. Text, not an enum |
| `clarification` | jsonb NOT NULL | the `Clarification` (question, options, mode) and the state the resumed run needs |
| `status` | text NOT NULL | `CHECK (status IN ('open', 'answered', 'abandoned'))` |
| `answer_option_ids` | text[] NULL | |
| `created_at`, `answered_at` | timestamptz | |

Index: `(user_id, conversation_id, status)`.

**Inside the payload.** Activities live in the sport payload, not in a table. That keeps a new sport migration-free (G4). Each payload model belongs to its plugin, and only the plugin's Pydantic model reads or writes it. A grade is stored per activity as `{original, scale, french, ordinal}` (B20). `original` and `scale` are the truth. `french` and `ordinal` are derived and can be recomputed by an upcaster if the conversion table changes (ADR-0006 invariant 6, ADR-0016).

**Not stored in M1.**

- `TrainingLoad`. It is derived from the session, and M2 may change the formulas (ADR-0006 Consequences). M2 decides whether to cache it, in a table it can rebuild at any time.
- Edit history of a session. An edit overwrites the row and bumps `updated_at`.
- Soft delete. B4 delete is a hard delete, and the UI confirms it first.

### Invariants

1. A training session never references a conversation, a chat message, a draft or a clarification. Purging conversations (B26) can't touch it.
2. Every row of `training_sessions` has at least one `training_session_sources` row carrying the athlete's original text (B2).
3. Only a confirm or an edit the athlete makes writes `training_sessions` (F2, the CLAUDE.md "AI changes are drafts" rule). A clarification writes nothing to it (ADR-0015 invariant 3).
4. A payload change bumps `payload_version`, and older versions are upcast on read in the plugin. A migration never rewrites payload JSON in bulk.
5. `sport_id` and `purpose` are text checked in code. Adding a sport or a clarification purpose needs no migration.
6. Every query filters by `user_id`, including the ones that join through `session_id` or `conversation_id` (ADR-0004 invariant 2).

## Alternatives considered

- An `activities` table with typed columns — easier SQL for M2 stats, but every new sport or activity field becomes a migration on production data (G4, G13). JSONB plus Postgres JSON path queries cover M2's needs. A GIN or expression index can be added later without rewriting data.
- `raw_text` on the session row (ADR-0006's original envelope) — loses the second and third original texts when logs merge (B16).
- Drafts held only in memory or in the SSE stream — a reload loses them, and a choice card's answer has nothing to resume (ADR-0015).
- A `deleted_at` soft delete — every query would need the filter, and G7 deletion would get harder to reason about. That isn't worth it before an "undo" exists in the PRD.

## Consequences

- ADR-0006's envelope list changes: `raw_text` and `source` move to `training_session_sources`, and `timezone` is added. Amend ADR-0006's Consequences when this ADR is Accepted.
- **Review checklist for the migration PR (ADR-0024):**
  - Every FK has the stated `ON DELETE`.
  - Every CHECK from this ADR is present.
  - The tenancy test covers each table.
  - The G7 test proves account deletion empties all four tables.
  - A purge test proves conversations go and sessions stay.
- The first logging ticket that persists a draft creates all four tables in one migration, so the schema is reviewed as a whole. (Owner-directed 2026-10-12: this was "confirms a draft", but a draft is stored before it can be confirmed.)
- M1 grades, owner-directed 2026-10-12. B20 conversion is M5 in PRD 0004, so climbing payload v1 stores each grade's `original` and `scale` and leaves `french` and `ordinal` null. When M5's conversion lands, an upcaster fills them on read (invariant 4). No migration and no bulk rewrite is needed.
- Owner-directed 2026-10-13, at #154: every reference from these tables to a user-owned row is keyed with `user_id` too, as `chat_messages` → `conversations` already is (ADR-0021 invariant 1). That covers `session_drafts` and `clarifications` → `conversations`, `chat_messages` and `session_drafts`, `session_drafts` → `training_sessions`, and `training_session_sources` → `training_sessions`. The targets get `UNIQUE (id, user_id)`: `chat_messages` (an additive constraint on an existing table), `training_sessions` and `session_drafts`. A row then can't point at another user's data, and one user's account deletion can't cascade into another's rows (invariant 6, ADR-0004 invariant 2). Each `ON DELETE` stays as listed above. The two `SET NULL` keys clear only their session column (`SET NULL (session_id)`, PostgreSQL 15+), since `user_id` is NOT NULL. Found by #154's skeptic review.
- Owner-directed 2026-10-21, at #163: changing a pending draft's sport on its card (B23) replaces the draft in place. The same `session_drafts` row keeps its ID, `message_id` and status, and its `sport_id`, `draft`, `payload_version` and `extends_session_id` are overwritten with the new sport's extraction of the same log, B16 checked again. Only a pending row changes, and no session is written (invariant 3). The chat message that introduced the draft keeps its own sport and template, so the card stays under that reply.
- M2 adds indexes for the stats queries it actually runs. They are additive, so ADR-0024 allows them.
- ⚠ To check at the first M2 ticket: whether JSON path queries on `payload` meet the stats view's latency on realistic data. If not, a derived, rebuildable table (M2's call), never a rewrite of `training_sessions`.
- Amended by [ADR-0027](0027-venues-gym-grading-and-device-location.md), accepted 2026-10-25: `training_sessions` and `session_drafts` gain a nullable `athlete_venue_id`, keyed with `user_id` as the 2026-10-13 amendment requires, with `ON DELETE SET NULL (athlete_venue_id)`. Adding it is an expand-only migration (ADR-0024).
