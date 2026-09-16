# ADR 0011 — Knowledge base and ingestion (outline)

| | |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Related | PRD-0001 (B7, B8, G5), ADR-0004, ADR-0007, ADR-0010 |

## Context

Expert answers (B8) need a curated corpus: books, articles, transcripts and research papers about the supported sports, fed in through an ingestion pipeline (B7). Storage and retrieval shape the schema now; ingestion details can wait until M3.

## Decision

**Storage:** PostgreSQL with pgvector (ADR-0004).

- `knowledge_sources`: manifest ID, title, author, source type, evidence tier, license note, content hash.
- `knowledge_chunks`: source, text, `VECTOR(dim)` embedding, embedding model, sports, topics, `language` (the G5 seam), citation locator (page, chapter, timestamp) and a full-text `tsvector`.
- An HNSW index with `vector_cosine_ops`.

**Evidence tiers:** peer-reviewed > coaching book > article / transcript. Retrieval and answers can weigh and show the tier.

**Embeddings:** OpenRouter `/api/v1/embeddings`, with the model and dimension pinned in settings and stored on every chunk. Changing the embedding model means a re-embed job.

**Retrieval:** hybrid — vector similarity plus PostgreSQL full-text search, filtered by sport and topic.

**Ingestion pipeline:** a named, admin-run CLI job, idempotent by content hash:
register the source in a committed manifest → extract → clean → chunk → embed → store.
Parsers, chunking strategy, OCR and transcript handling are decided in a follow-up ADR at the start of M3.

### Invariants

1. Answers built on knowledge-base content cite their sources (B8).
2. User training data never enters the shared knowledge base.
3. Source files stay out of git (`data/corpus/` is gitignored); only the manifest, with license notes, is committed.
4. Every chunk records the embedding model that produced it; chunks from different models are never searched together.

## Consequences

- A follow-up ADR at M3 details ingestion. The `/ingest-knowledge` skill arrives with it.
- `search_knowledge` is exposed as an MCP tool (ADR-0010).
