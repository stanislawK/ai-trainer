# ADR 0033 — Knowledge ingestion and admin upload

| | |
|---|---|
| Status | Proposed |
| Date | 2026-10-30 |
| Related | PRD-0010 (B7, B8, G1, G5, G10), ADR-0005, ADR-0010, ADR-0011 (details its ingestion; supersedes invariant 3's committed manifest), ADR-0018, ADR-0020, ADR-0023, ADR-0031 |

## Context

ADR-0011 fixes how knowledge is stored and retrieved: pgvector chunks, evidence tiers, hybrid search, and embeddings through OpenRouter. It leaves ingestion to a follow-up ADR. It also assumes an admin-run CLI fed from a committed manifest and a gitignored `data/corpus/`.

The owner wants three things (2026-10-29):
- documents uploaded to production by an admin, never through the GitHub repository;
- a way to research high-quality sources without re-ingesting what is already there;
- the catalogue's English and Polish exercise texts (ADR-0031) searchable by the agent.

Agents never connect to the production host (ADR-0023), so ingestion has to run inside the app.

## Decision

**Object storage.** `ObjectStoragePort` (put, get, delete, presigned get) with one S3-compatible adapter for Cloudflare R2. The library is chosen at the infra ticket through context7. Two buckets, configured in `Settings` with their keys in `.env`:
- `uploads`: private. Admin-uploaded source files.
- `media`: public through a custom domain. Catalogue media only (ADR-0031).

Neither holds user training data.

**Admin upload.** `/admin/knowledge` is visible only to `ADMIN_EMAILS` (ADR-0005). It lists sources with their status, chunk count and embedding model.
- **The upload form** takes:
  - the file: PDF, EPUB, Markdown, plain text, or a transcript as `.txt`, `.vtt` or `.srt`, under `knowledge_max_upload_mb`;
  - the source's details: title, author, source type, evidence tier, language, sports, topics, license note and source URL.
- **Actions:** re-ingest and delete. Delete removes the file, the source and its chunks.
- Every form is CSRF-protected.

**Manifest in the database.** `knowledge_sources` is the manifest:
- It gains `storage_key`, `status` (`uploaded`, `ingesting`, `ingested`, `failed`), `error`, `uploaded_at` and `ingested_at`.
- A content hash already identifies a source (ADR-0011), so re-uploading the same file is a no-op.
- Nothing about sources is committed to git any more. That supersedes ADR-0011 invariant 3's manifest, while its "files stay out of git" stands.

**Ingestion job.**
- **Running:** an in-process worker picks up `uploaded` sources. A durable status row makes it resumable after a restart, as ADR-0028's background reads are.
- **Steps:** extract text with a permissively licensed parser (checked at the ticket), clean, chunk on headings with a citation locator (page, chapter, timestamp), embed in batches, store, set `ingested`.
- **Chunk size:** chosen at the ingestion ticket on retrieval checks over the first sources.
- **Accounting:** each embedding call writes an `llm_calls` row (ADR-0018) with the admin as the acting user.

**Catalogue texts.** The same job ingests `exercise_texts` (ADR-0031) as `source_type = catalogue`, one chunk per exercise and language, tagged with `exercise_id`, `language` and `sports = {gym}`. It runs again whenever `import-exercises` changes a text.

**Embeddings.**
- **Settings:** `EMBEDDING_MODEL` and `EMBEDDING_DIM`.
- **Model:** must embed English and Polish into one space, so a Polish chunk answers an English question and the other way round.
- **Choice:** made by a spike on the OpenRouter MCP (`list-models` with embedding output, price, the dimension and HNSW fit). The ADR records it in place, citing the tool and date. pgvector's HNSW indexes `vector` up to 2,000 dimensions and `halfvec` up to 4,000 ([pgvector](https://github.com/pgvector/pgvector#hnsw)).

**Retrieval.** `search_knowledge(query, sport?, topics?, exercise_id?, limit)` is an MCP tool (ADR-0010):
- It fuses a vector top-k (cosine, HNSW) with a full-text top-k by reciprocal rank fusion, the method pgvector's README points to.
- A chunk's `tsvector` uses `english` for English and `simple` for Polish, which has no built-in stemmer (ADR-0031).
- Results carry the source's title, author, tier and locator for citing (B8).

**Curation.** The `/research-knowledge` skill:
- finds candidate sources for a topic, ranked by evidence tier: peer-reviewed, then coaching books, then expert talks, podcasts and articles;
- rejects content without credentials or evidence, such as popular but unvetted influencer videos;
- checks `knowledge_sources` for what is already covered;
- writes a shortlist for the owner, who obtains the files and uploads the ones worth keeping.

Agents don't upload.

### Invariants

1. Only an `ADMIN_EMAILS` user can upload, re-ingest or delete a source.
2. Source files live in the `uploads` bucket, never in git or on the image.
3. User training data, messages and profiles never enter a bucket, a source or a chunk (ADR-0011 invariant 2).
4. Every chunk records its embedding model, and chunks from different models are never searched together (ADR-0011 invariant 4).
5. Every source has an evidence tier and a license note before it is ingested.

## Consequences

- ADR-0011 stays the storage decision. Its "follow-up ADR at M3" is this one, and its `/ingest-knowledge` skill becomes `/research-knowledge` plus the upload page.
- ⚠ `EMBEDDING_MODEL` and `EMBEDDING_DIM` are open until the spike. The spike needs the OpenRouter MCP authorized (`/mcp`).
- ⚠ The `uploads` bucket is the only copy of a source file. ADR-0023's nightly backup covers the database, not R2. Whether to copy the bucket too is decided at the infra ticket.
- `deploy/` and `.env.example` gain the R2 settings. The owner creates the buckets and keys; agents never touch Cloudflare (ADR-0023).
- `/admin/knowledge` needs a Claude Design mock before its web ticket (ADR-0019).
