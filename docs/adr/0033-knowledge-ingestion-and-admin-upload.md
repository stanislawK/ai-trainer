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
- **Choice:** `EMBEDDING_MODEL = qwen/qwen3-embedding-8b`, `EMBEDDING_DIM = 1024`, stored as `vector(1024)`, chosen by the owner at #417 (2026-11-02). pgvector's HNSW indexes `vector` up to 2,000 dimensions and `halfvec` up to 4,000 ([pgvector](https://github.com/pgvector/pgvector#hnsw)). The model's native 4,096 fits neither, so 1,024 is requested.
- **Every call:**
  - sends `dimensions = 1024` with `require_parameters`, so an ignored parameter fails instead of storing the wrong length;
  - is pinned to the DeepInfra upstream with no fallback. Three upstreams serve the model at different prices and speeds (SiliconFlow reports fp8, the other two don't report a precision), and ADR-0011 invariant 4 tracks the model, not the upstream. p99 over 30 minutes was 5.3 s on DeepInfra and 30.9 s on Nebius (`list-model-endpoints`, 2026-11-02).
- **Query instruction:** a query carries an `Instruct:` line, in English, and a document carries none ([model card](https://huggingface.co/Qwen/Qwen3-Embedding-8B)). Its wording is a prompt template file (ADR-0008), written at the `search_knowledge` ticket.
- **Stable:** no preview or `-exp` marker and no expiry date; on OpenRouter since 2025-10-28 (`list-models`, 2026-11-02). Weights are Apache-2.0 per the model card, though the OpenRouter description calls the series proprietary.
- **Cost:** $0.01/M tokens on DeepInfra (`list-model-endpoints`, 2026-11-02). The test below billed 85,863 tokens for 300 exercises in two languages plus their names, which scales to about 376,000 tokens and **$0.004** for the catalogue's 1,315 exercises.

**How it was chosen.** Nine models from the 39 embedding models in `list-models` (2026-11-02) were run once on catalogue text only, with the owner's approval, for $0.035 in total. Free tiers, previews, English-only and code models were among those left out. The throwaway script took a seeded sample of 300 exercises from the pinned dataset. R@10 is how often the right exercise was in the top 10 of those 300.

| Model | $/M | English name → Polish text | English text → Polish text | Polish text → English text | English name → English text |
|---|---|---|---|---|---|
| `google/gemini-embedding-001` | 0.15 | 0.92 | 1.00 | 1.00 | 0.96 |
| **`qwen/qwen3-embedding-8b`** | 0.01 | 0.75 | 0.96 | 0.98 | 0.94 |
| `openai/text-embedding-3-large` | 0.13 | 0.75 | 0.97 | 0.95 | 0.92 |
| `perplexity/pplx-embed-v1-4b` (cut from 2,560 to 1,024 by the script) | 0.03 | 0.63 | 0.98 | 0.95 | 0.82 |
| `voyageai/voyage-4` | 0.06 | 0.61 | 0.96 | 0.97 | 0.85 |
| `voyageai/voyage-4-lite` | 0.02 | 0.34 | 0.74 | 0.75 | 0.72 |
| `baai/bge-m3` | 0.01 | 0.33 | 0.75 | 0.80 | 0.73 |
| `intfloat/multilingual-e5-large` | 0.01 | 0.32 | 0.76 | 0.65 | 0.78 |
| `openai/text-embedding-3-small` | 0.02 | 0.18 | 0.47 | 0.49 | 0.82 |

Gemini scored highest, and the owner chose Qwen3-8B with these results in hand. Qwen3-8B costs a fifteenth of Gemini per token, has open weights and has three upstreams against Google's one. For "Rising straight arms on both sides" (ADR-0031), Qwen3-8B ranked `dumbbell lateral raise` first in both languages.

**Polish evidence.**
- **Measured here:** the table above, on this project's own pairs.
- **Published:** the best multilingual score among the rows read on PL-MTEB's 30 tasks, and 59.21 nDCG@10 on its 11 Polish retrieval tasks, against 52.43 for `multilingual-e5-large` ([PL-MTEB, arXiv 2405.10138 v2](https://arxiv.org/abs/2405.10138), Table 2). The arXiv page says "Accepted for ACL 2026 Findings", which was not confirmed in the proceedings. Its tasks are Polish only, so it says nothing about crossing languages.
- **Unproven:** how English questions find Polish passages in real use. One run on translated instructions and exercise names over 300 candidates is an easy test, not the full 1,315, and the right Polish text ranked first for only 0.34 of English names.
- **Risk:** Polish-only content ranks low for English questions, so `search_knowledge` misses it. Catalogue exercises also have an English chunk, so they are less exposed than a Polish-only document.
- **Settled by:** the retrieval checks at the ingestion ticket. If they fail, the way out is another model and a re-embed job (ADR-0011).

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

- **Research first**, owner-directed 2026-10-31. Two research tickets write [docs/research/knowledge-base.md](../research/knowledge-base.md) (parsers, chunking, citation locators, evidence handling) and [docs/research/hybrid-search.md](../research/hybrid-search.md) (fusion, HNSW, English and Polish full-text, cheap retrieval checks) from trusted sources only. The embedding spike and every implementing ticket cite them. A finding that contradicts this ADR is proposed through `/update-docs`.
- `search_knowledge`, with the catalogue texts ingested, is what later improves the exercise shortlist (ADR-0031). That improvement is measured here, not in M1g.
- ADR-0011 stays the storage decision. Its "follow-up ADR at M3" is this one, and its `/ingest-knowledge` skill becomes `/research-knowledge` plus the upload page.
- A `search_knowledge` query can be an athlete's question, so it reaches the embedding upstream as it reaches a chat upstream (ADR-0007). What it costs in time, and what a search does when the pinned upstream is down, are for the `search_knowledge` ticket (PRD-0010 G14).
- ⚠ The `uploads` bucket is the only copy of a source file. ADR-0023's nightly backup covers the database, not R2. Whether to copy the bucket too is decided at the infra ticket.
- `deploy/` and `.env.example` gain the R2 settings. The owner creates the buckets and keys; agents never touch Cloudflare (ADR-0023).
- `/admin/knowledge` needs a Claude Design mock before its web ticket (ADR-0019).
