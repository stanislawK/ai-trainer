# ADR 0024 — Production data and migration safety

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-11 |
| Related | PRD-0004 (G7, G13), ADR-0001, ADR-0004, ADR-0013, ADR-0021, ADR-0022, ADR-0023 |

## Context

Until now the database has been disposable. `docker compose down -v` was a fine fix, and `.claude/rules/migrations.md` asks that "every migration upgrades and downgrades cleanly". That's the right rule for a dev database. It's the wrong one once real athletes have logged sessions, because a `downgrade()` that drops a table and a `DROP COLUMN` shipped in the same release as the code change are both silent data loss. From the first production deploy (ADR-0023), the owner's rule is: **production data is never deleted by a change** (G13). Every later migration has to respect that, and the training-session schema (ADR-0022) has to be right from the start.

## Decision

- **Expand → migrate → contract.** A schema change that removes or narrows anything ships in at least two releases:
  1. **Expand** (release N): add the new column, table or constraint `NOT VALID`. Backfill. The code writes both the old and new shape, or only the new one, and still reads the old.
  2. **Contract** (release N+1 or later, after N is live in production): drop or narrow the old thing in its own migration.

  A contract migration is called out in its ticket title (`… (contract)`). It needs the owner's explicit yes at the plan gate, and a verified backup from that day.
- **Forbidden in an expand release:** `DROP TABLE`, `DROP COLUMN`, `RENAME` of a table or column, a type change that can lose data, `TRUNCATE`, and a `DELETE` or `UPDATE` without a `WHERE` on user data. Also forbidden is a NOT NULL added without a default or a backfill.
- **Additive changes ship in one release:** new tables, nullable columns, indexes (`CREATE INDEX CONCURRENTLY` once a table is large), and `CHECK … NOT VALID` followed by `VALIDATE`.
- **Fix forward only.** `downgrade()` still exists for development and tests (ADR-0004), but it's never run against production. A bad migration is fixed with a new one, and the pre-deploy backup is the last resort (ADR-0023).
- **Data migrations** (backfills, payload upcasts) are idempotent, scoped by `user_id` where they touch user data, and must finish in seconds on production volume. Anything slower is a separate CLI command, run after the deploy. Sport payloads are never rewritten in bulk; they're upcast on read (ADR-0022 invariant 4).
- **CI gates** in `ci.yml`, required on `main` (ADR-0023):
  - **[squawk](https://squawkhq.com/docs/rules) lints the SQL** of new revisions, rendered with Alembic's offline mode (`alembic upgrade <base_head>:head --sql`). Its default rules catch `ban-drop-column`, `ban-drop-table`, `renaming-column`, `adding-not-nullable-field`, `changing-column-type` and non-concurrent index builds. A deliberate contract step carries a `squawk-ignore` comment with its reason and ticket number.
  - **An upgrade-with-data test:**
    1. Migrate a fresh database to `main`'s head.
    2. Seed it with `tests/migrations/seed.py`: at least one row in every user-owned table, for two users.
    3. Upgrade to the branch head.
    4. Assert that every seeded row is still there with its values. Only the columns a contract migration explicitly removes are exempt.
- **Agents never touch production.** No agent has a credential for the server or the production database. The `PreToolUse` hook from ADR-0023 also denies `ssh` and `scp` to the production host. Destructive commands are for the dev stack only: `docker compose down -v`, `alembic downgrade`, and `DROP` or `TRUNCATE` in `psql`.
- **Gates.** A ticket whose diff touches `migrations/` gets a **data-safety question** at the plan gate and again at the review gate. The question asks: is this expand or contract, what happens to existing rows, and which backfill. `/apply-ticket` carries it.

### Invariants

1. No migration that reaches production deletes or irreversibly rewrites user data, unless it's a contract step the owner approved for data that a previous production release stopped using.
2. Every migration passes squawk and the upgrade-with-data test before it can merge.
3. Production is never downgraded. It's fixed forward or restored from backup.
4. The code in release N runs correctly on the schema of release N+1 (expand-only). That's what makes the app rollback in ADR-0023 safe.
5. No agent can reach the production host or database.

## Alternatives considered

- Keep "upgrades and downgrades cleanly" as the safety net — a downgrade that drops a table is exactly the loss G13 forbids.
- Review discipline only, without CI checks — the owner asked for a hard gate. squawk and the data test catch what a tired review misses.
- A blue/green database or logical replication — far beyond one POC instance. A backup before migration (ADR-0023) gives the restore path.

## Consequences

- **`.claude/rules/migrations.md`:**
  - "upgrades and downgrades cleanly" is replaced by this ADR's rules.
  - Its scope gains `tests/migrations/**`.
- **`CLAUDE.md` non-negotiables:** gain "Production data is never deleted by a change: expand/contract, forward-only (ADR-0024)".
- **`.claude/skills/apply-ticket/SKILL.md`:** the plan and review gates gain the data-safety question when `migrations/` is touched.
- **`.claude/skills/create-tickets/SKILL.md`:** a contract migration is its own ticket.
- **ADR-0004:** its Consequences note that invariant 3 ("never edit an applied migration") now means applied anywhere, including production.
- **Before the first production deploy:** the CI gates land, and the seed covers every table that exists then.
- ⚠ To verify at the CI ticket: whether Alembic offline `--sql` rendering works for every existing revision. The vector-extension and backfill revisions may need `context.is_offline_mode()` guards. If one can't render, lint only revisions newer than it.
