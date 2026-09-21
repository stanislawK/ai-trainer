---
paths:
  - "migrations/**"
---

# Migration rules (ADR-0004)

- Autogenerate with Alembic, then review every line; delete noise and give the revision a descriptive name.
- Never edit an applied migration; fix forward with a new one.
- A new user-owned table gets `user_id` NOT NULL, a foreign key to `users` with `ON DELETE CASCADE` (G7), and an index.
- Vector columns use `VECTOR(dim)` matching the embedding dimension in settings, with an HNSW index on `vector_cosine_ops` (ADR-0011).
- Every migration upgrades and downgrades cleanly; integration tests prove it.
