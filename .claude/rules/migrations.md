---
paths:
  - "migrations/**"
  - "tests/migrations/**"
---

# Migration rules (ADR-0004, ADR-0024)

- Autogenerate with Alembic, then review every line; delete noise and give the revision a descriptive name.
- Never edit an applied migration; fix forward with a new one.
- A new user-owned table gets `user_id` NOT NULL, a foreign key to `users` with `ON DELETE CASCADE` (G7), and an index.
- Vector columns use `VECTOR(dim)` matching the embedding dimension in settings, with an HNSW index on `vector_cosine_ops` (ADR-0011).
- **Production data is never deleted by a change (ADR-0024, G13).** Expand → migrate → contract:
  - **Expand.** Add things: new tables, nullable columns, indexes, `CHECK … NOT VALID` then `VALIDATE`. Backfills must be idempotent and finish in seconds.
  - **Contract.** `DROP`, `RENAME`, narrowing a type, and a NOT NULL without a backfill all count. A contract step ships in its own later ticket, after the release that stopped using the data, with a `squawk-ignore` comment giving its reason and ticket.
- Sport payload JSON is never rewritten in bulk; it is upcast on read (ADR-0022).
- A sport's activity tables are named `<sport>_*`, ship with that sport's tickets, and follow the user-owned table rules: `user_id` CASCADE, every reference keyed with `user_id`, covered by the tenancy and account-deletion tests. Core tables never gain sport-specific columns (ADR-0032).
- Shared reference tables (the exercise catalogue, ADR-0031) have no `user_id`; rows an athlete owns in them (`owner_user_id`, `user_id`) are nullable FKs with `ON DELETE CASCADE`.
- `downgrade()` is written for dev and tests, and every migration upgrades and downgrades cleanly on an empty database. Production is never downgraded; it is fixed forward.
- `make squawk` renders the revisions new on the branch offline and lints them. A deliberate contract step puts `# squawk-ignore <rule>[, <rule>]: <reason> (#<ticket>)` in its revision file; a comment without a reason and ticket fails. A revision must render with `alembic upgrade <down>:<rev> --sql` (a body wholly guarded by `context.is_offline_mode()` renders no SQL and fails). The ignore covers the whole revision file. A concurrent index (`op.create_index(..., postgresql_concurrently=True, if_not_exists=True)` inside `op.get_context().autocommit_block()`) needs `if_not_exists=True`, or squawk's `prefer-robust-stmts` fires outside a transaction. Run `git fetch origin main` first so the base is current.
- Every migration passes squawk and the upgrade-with-data test (`tests/migrations/`) before merge.
