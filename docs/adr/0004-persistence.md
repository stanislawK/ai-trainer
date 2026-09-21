# ADR 0004 — Persistence

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (G1, G7, B1–B6, B8–B13, B19), ADR-0003, ADR-0005, ADR-0011, ADR-0016 |

## Context

The app stores training sessions, plans, chat history and a vector knowledge base (B1–B13), for many users with strict isolation (G1) and full account deletion (G7).

## Decision

- **PostgreSQL 18 with pgvector 0.8.x**, image `pgvector/pgvector:pg18-trixie`. Compose sets `shm_size` at least as large as `maintenance_work_mem`, so parallel HNSW index builds don't fail.
- **SQLAlchemy 2.0 ORM** with typed declarative mapping (`Mapped[...]`, `mapped_column`), an async engine on `postgresql+psycopg` (psycopg 3), and `async_sessionmaker(expire_on_commit=False)`.
- **pgvector:** `pgvector.sqlalchemy.VECTOR` columns; `register_vector_async` runs on every new connection (a `connect` event on `engine.sync_engine`).
- **Alembic** in `migrations/` owns every schema change. Autogenerate, then review.
- Repositories live in `adapters/` and implement application ports. ORM models never leave the adapter layer; repositories return domain models.
- Sport payloads are stored as JSONB (ADR-0006).
- **Reference tables** (`crags`, `routes`, `places`, ADR-0016) are deliberately not user-owned: they hold public data about the world, shared by everyone. Invariant 1 does not apply to them, and account deletion has nothing to remove from them (B19).

### Invariants

1. Every user-owned table has `user_id` NOT NULL, a foreign key to `users` with `ON DELETE CASCADE` (G7), and an index.
2. Every repository method that touches user-owned data takes `user_id`. There is no unscoped query API.
3. Never edit an applied migration; fix forward with a new one.
4. The schema changes only through Alembic.
5. Timestamps are timezone-aware (`timestamptz`, UTC).

## Consequences

- `.claude/rules/migrations.md` carries these rules.
- Integration tests run against a real PostgreSQL (ADR-0013). Every repository has a tenancy test: user A cannot read or change user B's rows.
- The first migration creates the `vector` extension.
