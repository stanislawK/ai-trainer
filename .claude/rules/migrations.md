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
- `downgrade()` is written for dev and tests, and every migration upgrades and downgrades cleanly on an empty database. Production is never downgraded; it is fixed forward.
- Every migration passes squawk and the upgrade-with-data test (`tests/migrations/`) before merge.
