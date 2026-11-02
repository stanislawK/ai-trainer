# ADR 0033 — Knowledge ingestion and admin upload

| | |
|---|---|
| Status | Accepted |
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

**Object storage.** `ObjectStoragePort` (put, get, delete, presigned get) with one S3-compatible adapter for Cloudflare R2, built on [`obstore`](https://github.com/developmentseed/obstore), chosen by the owner at #420 (2026-11-02). Two buckets, configured in `Settings` with their keys in `.env`:
- `uploads`: private. Admin-uploaded source files. Settings: `OBJECT_STORAGE_ENDPOINT`, `OBJECT_STORAGE_REGION` (default `auto`), `OBJECT_STORAGE_ACCESS_KEY_ID`, `OBJECT_STORAGE_SECRET_ACCESS_KEY`, `OBJECT_STORAGE_UPLOADS_BUCKET` and `OBJECT_STORAGE_TIMEOUT_SECONDS`. All optional until a feature needs them, so a release never waits on new keys; a partial set refuses to start.
- `media`: public through a custom domain. Catalogue media only (ADR-0031). Its settings come with the ticket that first uses it.

Neither holds user training data.

- **Library:** `obstore` is natively async, ships its own types, and has no dependencies on Python 3.14 (one Rust wheel). `aiobotocore` would bring `aiohttp` and a `botocore` held to a three-patch range, and needs a stubs package for `mypy --strict`. `obstore` is pre-1.0 (0.12.1), so a breaking change is contained by keeping it in this one adapter.
- **Local service:** `make start` runs [versitygw](https://github.com/versity/versitygw) v1.10.0, pinned by digest, so development needs no Cloudflare. It checks signatures and link expiry. RustFS and s3proxy passed the same probe (`obstore` round trip, tampered and expired links, a wrong secret) on 2026-11-02, with larger images and a client call to create a bucket. MinIO's community edition is [archived](https://github.com/minio/minio).
- **Backup:** the `uploads` bucket is **not copied**, decided by the owner at #420. Chunks and their text are in Postgres, which the nightly backup covers (ADR-0023), so search survives a lost file; re-ingesting that source needs the file uploaded again. The owner keeps the originals (`deploy/RUNBOOK.md` section 14). A bucket lock isn't an option, since it would refuse the delete below. The risk accepted: the app's token can delete objects, so a stolen token or a mistaken admin delete loses a file until it is uploaded again.

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
- **Settings:** `EMBEDDING_MODEL`, `EMBEDDING_DIM` and `EMBEDDING_PROVIDER_PREFERENCES`, the last in ADR-0026's shape.
- **Model:** must embed English and Polish into one space, so a Polish chunk answers an English question and the other way round.
- **Choice:** `EMBEDDING_MODEL = qwen/qwen3-embedding-8b`, `EMBEDDING_DIM = 1024`, stored as `vector(1024)`, chosen by the owner at #417 (2026-11-02). pgvector's HNSW indexes `vector` up to 2,000 dimensions and `halfvec` up to 4,000 ([pgvector](https://github.com/pgvector/pgvector#hnsw)). The model's native 4,096 fits neither, so 1,024 is requested.
- **Every call:**
  - sends `dimensions = 1024` and checks the length of every returned vector, failing the batch on a mismatch. DeepInfra returned 1,024 in the test. `require_parameters` isn't relied on: the MCP lists no supported parameters for any embedding endpoint, and its failure path was never seen to run;
  - stays on DeepInfra through `{"only": ["deepinfra"]}`. ADR-0026's preferences keep fallbacks on and refuse `order`, but a one-name `only` leaves nothing to fall back to. The test itself used `order` with fallbacks off and was served by DeepInfra. Three upstreams serve the model at different prices and speeds (SiliconFlow reports fp8, the other two report no precision). p99 was 5.3 s on DeepInfra in the one 30-minute window read (`list-model-endpoints`, 2026-11-02).
- **Limits:** 32,768 input tokens on DeepInfra (`list-model-endpoints`, 2026-11-02), far above any catalogue row. The chunk size stays with the ingestion ticket.
- **Query instruction:** a query carries an `Instruct:` line, in English, and a document carries none ([model card](https://huggingface.co/Qwen/Qwen3-Embedding-8B)). Its wording is a prompt template file (ADR-0008), written at the `search_knowledge` ticket. `input_type` was not tested on any model, so it isn't sent.
- **Stable:** no preview or `-exp` marker and no expiry date; on OpenRouter since 2025-10-28 (`list-models`, 2026-11-02). Weights are Apache-2.0 per the model card, though the OpenRouter description calls the series proprietary.
- **Cost:** $0.01/M tokens on DeepInfra (`list-model-endpoints`, 2026-11-02). The test below billed 85,863 tokens for 300 exercises in two languages plus their names, which scales to about 376,000 tokens and **$0.004** for the catalogue's 1,315 exercises.

**How it was chosen.** Nine models from the 39 embedding models in `list-models` (2026-11-02) were run once on catalogue text only, with the owner's approval, for $0.035 in total. Free tiers, previews, English-only and code models were among those left out. The throwaway script took a seeded sample of 300 exercises from the pinned dataset. R@10 is how often the right exercise was in the top 10 of those 300. This applies the checks in [docs/research/hybrid-search.md](../research/hybrid-search.md) on the project's own pairs.

The comparison isn't fully like for like. The two text-to-text columns compare documents and send no instruction. For the name columns, Qwen3-8B's query carried `Instruct: Given an exercise name, retrieve the passage describing that exercise`, and `multilingual-e5-large` used its `query:` and `passage:` prefixes, while the others sent the name alone. The real instruction will differ, so 0.75 may not carry over.

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

Gemini scored highest, and the owner chose Qwen3-8B with these results in hand. Qwen3-8B costs a fifteenth of Gemini per token and has open weights. Its other upstreams give no redundancy at run time, since the pin gives that up and a DeepInfra outage stops ingestion and the vector leg; they are a way off DeepInfra, through a re-embed. For "Rising straight arms on both sides" (ADR-0031), Qwen3-8B ranked `dumbbell lateral raise` first in both languages.

**Polish evidence.**
- **Measured here:** the table above, on this project's own pairs.
- **Published:** none is relied on. No source read with a confirmed venue measures this model on Polish, and `docs/research/hybrid-search.md` asks for a confirmed venue before an ADR cites a paper.
- **Unproven:**
  - how English questions find Polish passages in real use. One run on translated instructions and exercise names over 300 candidates is an easy test, not the full 1,315, and the right Polish text ranked first for only 0.34 of English names;
  - how a short Polish question finds an English passage. The dataset has no Polish names, so no short Polish query was tested, and 0.98 above is a whole translated instruction as the query.
- **Risk:** content in the other language ranks low for a short question, so `search_knowledge` misses it. Catalogue exercises have a chunk in each language, so they are less exposed than a document in one language.
- **Settled by:** the retrieval checks at the ingestion ticket, on real questions in both directions and the same R@10 columns over all 1,315 exercises, with `gemini-embedding-001` as the baseline. No pass mark is set here; the owner decides on those figures. A paid run there needs the owner's yes. If they fail, the way out is another model and a re-embed job (ADR-0011), which costs more as sources accumulate.

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
4. Every chunk records its embedding model, and chunks from different models are never searched together (ADR-0011 invariant 4). Moving to another upstream counts as changing the model: it means a re-embed job, because a chunk doesn't record its upstream.
5. Every source has an evidence tier and a license note before it is ingested.

## Consequences

- **Research first**, owner-directed 2026-10-31. Two research tickets write [docs/research/knowledge-base.md](../research/knowledge-base.md) (parsers, chunking, citation locators, evidence handling) and [docs/research/hybrid-search.md](../research/hybrid-search.md) (fusion, HNSW, English and Polish full-text, cheap retrieval checks) from trusted sources only. The embedding spike (#417) applied the second one, and every implementing ticket cites them. A finding that contradicts this ADR is proposed through `/update-docs`.
- `search_knowledge`, with the catalogue texts ingested, is what later improves the exercise shortlist (ADR-0031). That improvement is measured here, not in M1g.
- ADR-0011 stays the storage decision. Its "follow-up ADR at M3" is this one, and its `/ingest-knowledge` skill becomes `/research-knowledge` plus the upload page.
- A `search_knowledge` query can be an athlete's question, so it reaches the embedding upstream as it reaches a chat upstream (ADR-0007). What it costs in time, and what a search does when the pinned upstream is down, are for the `search_knowledge` ticket (PRD-0010 G14).
- ⚠ The `uploads` bucket is the only copy of a source file in the system, since ADR-0023's nightly backup covers the database, not R2. See *Backup* in the Decision: the owner's originals are the second copy.
- `deploy/` and `.env.example` gain the R2 settings. The owner creates the buckets and keys; agents never touch Cloudflare (ADR-0023).
- **Object storage gotchas (#420):**
  - **A library message never leaves the adapter.** A refusal's text can quote the access key: versitygw's and s3proxy's did, on 2026-11-02. `obstore`'s error text also holds the request's URL. So the domain error carries only the operation and a closed reason, raised `from None`, and the log line names only those and the exception's type.
  - **A presigned link is a secret.** It holds the key ID and a signature, so nothing logs or prints one. `storage-check` opens links with `urllib`, because `httpx2` logs every request's URL at INFO.
  - **Credentials go through `credential_provider`, not as keys.** `obstore` reads `AWS_*` from the environment, and a stray `AWS_SESSION_TOKEN` was otherwise signed into every request and link, which would break them. The settings the adapter passes outrank the environment, so nothing else there can redirect it.
  - **Retries are bounded.** `obstore` defaults to 10 retries over 3 minutes, which an admin's upload would sit through. The adapter uses 2, within `OBJECT_STORAGE_TIMEOUT_SECONDS`.
  - **A delete is a bulk request.** `obstore` sends every delete as S3's DeleteObjects (`POST /<bucket>?delete`), never `DELETE`. `storage-check` exercises it, on R2 too.
  - **Links on R2.** They live 1 second to 7 days and can't use a custom domain ([R2 docs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/), read 2026-11-02), so they name the account's S3 endpoint. The adapter refuses a time outside those bounds.
  - **Links under `make start`.** A link made inside the app container names `storage:7070`, which a browser can't open. Nothing opens one in a browser yet; the admin page's ticket decides this if it needs links.
  - **versitygw's region.** It refuses a signature for any region but its own, so it runs with `--region auto`, R2's name.
  - **What CI proves.** CI has no S3 service, so the adapter's tests run on a small server in `tests/object_storage_support.py`, which doesn't verify signatures. A library bump is checked by hand with `make storage-check`.
- `/admin/knowledge` needs a Claude Design mock before its web ticket (ADR-0019).
