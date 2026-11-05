# ADR 0036 — Knowledge ingestion at scale

| | |
|---|---|
| Status | Accepted |
| Date | 2026-11-03 |
| Related | PRD-0011 (B7, B8, G1, G14), ADR-0005, ADR-0010, ADR-0011, ADR-0023, ADR-0029, ADR-0033 (amends its upload, licence, worker and "Agents don't upload" parts), [docs/research/knowledge-ingestion-at-scale.md](../research/knowledge-ingestion-at-scale.md) |

## Context

ADR-0033's admin form takes one file and its details per upload. The owner wants thousands of sources, mostly gym research, and an AI pipeline for the admin built on `/research-knowledge` (owner-directed 2026-11-03). The research document lays out the options. The owner's answers, 2026-11-03:
- **Agents:** they may propose sources, which the admin approves in bulk.
- **Commercial status:** the app is not commercial and won't be.
- **Parser trial:** none now.
- **Ingestion container:** "depends if it would be better". This ADR proposes it, with reasons.
- **Bot Fight Mode:** a scripted request from the owner's laptop reached `/health` with 200.
- **Europe PMC:** its limit is 10 requests per second per IP address, read by the owner in a browser.

## Decision

**Two new ways in, both ending as proposals.**
- **By identifier:** an admin, or an agent with an admin token, submits PMC IDs with tier, sports and topics.
  - The app reads each article's JSON metadata from PMC's Cloud Service bucket (`pmc-oa-opendata`, public, the retrieval route PMC permits). It refuses a retracted article or a licence off the admit list, and records the source as `proposed` with its licence fields.
  - After approval, the worker fetches the article's JATS XML into `uploads` and ingests it.
  - The app calls no other literature API. Discovery, through Europe PMC search within its 10 requests per second, happens in the skill on the admin's machine.
- **By file:** for books, PDFs and transcripts the owner holds, a token client stages the metadata and gets a presigned PUT to `uploads`. It uploads the bytes straight to R2's S3 endpoint, then registers the upload.
  - Registering runs ADR-0033's checks on the stored object (type by content, size, content hash, duplicates) and leaves the source `proposed`.
  - `ObjectStoragePort` gains `presigned_put`.
- **Approval:** `/admin/knowledge` lists proposals and approves or rejects them in bulk. Approving sets `uploaded`, and the worker picks only `uploaded` sources.
- **The form stays:** ADR-0033's single-file form stays for one-offs. A form upload by an admin is `uploaded` at once.

**Admin tokens.**
- **Creating and revoking:** an admin creates and revokes tokens on `/admin`.
- **The token itself:** 32 random bytes, shown once, stored as its SHA-256 with a display prefix, `expires_at` at most 30 days ahead, `revoked_at` and `last_used_at`.
- **Each request:** it sends `Authorization: Bearer`. Every request re-checks that the token's user is `active` and in `ADMIN_EMAILS`.
- **Scope, staging only:**
  - stage by identifier;
  - stage a file and register it;
  - list sources, so the skill can skip what is covered.
- **Routes:** these live under `/api/admin/knowledge/`, take JSON within ADR-0029's 256 KiB, and are limited to 120 requests per token per rolling minute.
- **Out of scope:** approve, delete, re-ingest and everything else.

**Skill.** A new `/stage-knowledge` takes a shortlist from `/research-knowledge` that the owner approved in chat. It fills each source's template from the source's own metadata and stages it through a committed client script that reads the token from the environment. It reports what it staged, and what the owner must obtain by hand. `/research-knowledge` returns PMC IDs, DOIs and licences where it finds them, and reads covered sources through the list route.

**Template.** Every way in validates one model: CSL-JSON fields (`type`, `title`, `author`, `issued`, `DOI`, `PMID`, `PMCID`, `ISBN`, `abstract`, `keyword`, `language`, `URL`, `container-title`), plus `evidence_tier`, `source_type`, `sports`, `topics` and the licence below. A JATS article fills most of it from itself.

**Licences.** The free-text license note becomes structured fields:
- `license_id`, from a closed set: `cc0`, `cc-by`, `cc-by-sa`, `cc-by-nc`, `cc-by-nc-sa`, `cc-by-nd`, `cc-by-nc-nd`, or `other`;
- `license_url`;
- `license_source`: `pmc`, `jats` or `admin`;
- the copyright statement, verbatim;
- an attribution string.

`other` requires a note saying what permission the admin holds. Because the app is non-commercial, every CC licence is admitted. SA and ND sources carry a flag in the list until a lawyer has read them (Consequences). Refused:
- PMC's `TDM` code;
- `implied-oa`;
- no licence;
- "free to read".

PMC sources carry "Source: NLM PMC" in their attribution.

**Ingestion.**
- **Where it runs:** in an `ingest` service in `compose.prod.yaml`, from the app's image with its own command, memory and CPU limits, and a concurrency of 1. This replaces ADR-0033's in-process worker, for three reasons:
  - parsing is CPU-bound Python, which would stall the single Uvicorn process's event loop and chat (G14);
  - a large document could exhaust the app container's 1 GB and take chat down with it;
  - a crash in its own container restarts the worker alone, and the status row resumes it.
- **Parsing:** each reader converts its input into one internal document model of ordered blocks (heading, paragraph, list, table, caption, cue). Each block carries its heading path and an anchor (page and page label, spine item, JATS section, cue time). One chunker walks that model. The readers:
  - JATS and EPUB use lxml;
  - PDF uses pypdf;
  - VTT and SRT use webvtt-py;
  - Markdown and text use the standard library.

  These follow knowledge-base.md recs 1–3, and Docling isn't adopted now.
- **Chunking:**
  - JATS splits on `<sec>`, EPUB on spine and headings, Markdown on headings, PDF on paragraphs within a page, and transcripts on cue windows ending at sentences;
  - one token budget applies, merging small siblings and splitting only what is too large;
  - the abstract is its own chunk, and a table stays whole with its caption;
  - reference lists and running headers are not embedded.
- **What is embedded and indexed:**
  - each chunk is embedded as `title — heading path` followed by its text;
  - the `tsvector` weights the title A, headings B and body D;
  - the size and these choices are settled on hybrid-search.md's retrieval checks.

  No model writes context into chunks.
- **Embedding calls:** they go in batches with backoff on 429, one `llm_calls` row per batch with the approving admin as the acting user. They sit outside ADR-0029's per-user hourly budget, which covers the app's text and decision gateways, and are bounded instead by the concurrency of 1 and the OpenRouter credit limit.
- **Bulk loads:** after a bulk load, the HNSW index is rebuilt with a raised `maintenance_work_mem`.

### Invariants

1. A source is ingested only after an admin acts in the app, by uploading through the form or approving a proposal. Tokens and agents create `proposed` sources only, and the worker never reads one.
2. A token's user is re-checked as `active` and in `ADMIN_EMAILS` on every request. The token is stored only as a hash and expires within 30 days. It can stage and list, nothing else.
3. File bytes never pass through a model, an MCP argument or the app's request body on the token routes. They go by presigned PUT, or are fetched by PMC ID.
4. Every source has an admitted licence with an attribution before it is ingested. A retracted article is refused.
5. A paper whose JATS is available is ingested from JATS, never from its PDF.
6. Ingestion runs outside the web process. A crash or a memory spike there never stops chat.
7. Nothing about an athlete enters a staging request, a PMC fetch, a shortlist or a source (ADR-0011 invariant 2).
8. Agents still never connect to the production host (ADR-0023). They reach production only through the token routes, with a token the owner placed in their environment.

## Alternatives considered

- **Import from a staging bucket** — the owner would need a second R2 token, and whether "Object Read & Write" can delete, or copy across buckets, is undocumented (research finding 2). A presigned PUT signed by the app needs neither.
- **Browser bulk upload** — a closed tab stops it, every file crosses Cloudflare's 100 MB cap and 30-second write timeout, and a large manifest exceeds 256 KiB (research finding 1).
- **An admin MCP server now** — files can't travel in MCP arguments, a custom verifier isn't conformant OAuth, and Google through `OAuthProxy` adds token storage and a consent screen (research finding 3). It waits for ADR-0010's token amendment, and would reuse these tokens.
- **Docling (slim or full)** — not now (owner, 2026-11-03). Full Docling's PDF path needs 2.4–6 GB (research finding 8).
- **Contextual Retrieval or other model-made enrichment** — only if the retrieval checks show the deterministic arms fall short. It needs a template, a model and `/update-docs`.

## Consequences

- **ADR-0033** is superseded, once this is accepted, in these parts:
  - "Agents don't upload";
  - the in-process worker;
  - the free-text license note;
  - the file-type list, which gains `jats`.

  The rest stands.
- **ADR-0005:**
  - invariant 1 gains the token routes as a second source of `user_id`;
  - invariant 2 gains bearer authentication on them;
  - invariant 3's CSRF check doesn't apply there, since a bearer header is never sent by a browser on its own.
- **ADR-0029** gains the per-token request limit and says that ingestion's embedding calls are outside the per-user budget. **ADR-0023:**
  - `compose.prod.yaml` gains the `ingest` service and its limits;
  - the database container's memory is sized for the target corpus;
  - a release that adds it is the owner's, as always.
- **Migrations, expand only:** the `proposed` and `rejected` statuses, the structured licence fields, the bibliographic identifiers (`pmcid`, `doi`, `pmid`, `isbn`), the `jats` file type, and an `admin_tokens` table. #422's `license_note` rows map to `other` with their note, and nothing is deleted (ADR-0024).
- **New tickets after acceptance (`/create-tickets`):**
  - the migration;
  - admin tokens, a UI ticket with a mock;
  - the staging routes with `presigned_put` and the PMC adapter;
  - proposals and bulk approval on `/admin/knowledge`, a UI ticket with a mock;
  - the JATS reader;
  - the `ingest` service;
  - the `/stage-knowledge` skill and its client script;
  - `/research-knowledge`'s identifiers and covered list.

  #423 to #427 are re-read against this ADR (the #423 comment of 2026-11-03).
- **`CLAUDE.md`, `.claude/rules/` and `.claude/skills/`** were patched at acceptance, 2026-11-03.
- **PRD:** the next revision records that the app is non-commercial (owner, 2026-11-03), since NC sources rest on it. If that ever changes, NC sources are removed by one query on `license_id`.
- ⚠ **A lawyer, before production ingests SA or ND sources at scale:**
  - whether a quoted chat answer is a quotation under Directive 2001/29/EC Art. 5(3)(d) and Polish art. 29;
  - whether a model's paraphrase counts as ND "Adapted Material";
  - whether verbatim quotes trigger SA.
- ⚠ **Open from the research:**
  - OpenRouter's embedding batch limits, read with `list-model-endpoints`;
  - how much memory the database needs per 100,000 chunks.

  Both are settled at the `ingest` ticket.
- **Amended at #454 (licence detection on form uploads, and the tokens page),** owner-directed 2026-11-03 at that ticket's plan gate and design gates, on the [Admin Staging mock](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Admin+Staging.dc.html). It is proposed for the owner's review with #454's PR and holds once that PR is merged. It refines Invariant 1 (below); the rest of the Decision and the other invariants stand.
  - **Why.** A file uploaded by hand carries no machine-readable licence, and the admin shouldn't have to know one. Papers staged by PMC ID already take theirs from PMC.
  - **The tokens page.** Tokens are created and revoked at `/admin/tokens`, a third Admin page beside Users and Knowledge (the "on `/admin`" above means under Admin).
  - **The sheet:**
    - the Licence select's first option, and its default, is **Detect from the file**;
    - picking a licence keeps the upload `uploaded` at once, as above;
    - with Detect, the upload is stored as `detecting`, with no licence.
  - **Detection:**
    - the `ingest` container picks `detecting` sources and runs only the file's reader and a deterministic match: no chunking, no embedding, no model;
    - the match reads only the front matter, where a source states its own licence: the first 3 pages of a PDF, the EPUB spine items before the first chapter, the JATS `<permissions>` block, or the first 2,000 words of a text or transcript file. A CC notice further in, such as one on an embedded figure, is ignored;
    - it looks for CC licence URLs (`creativecommons.org/licenses/<code>/<version>`, `creativecommons.org/publicdomain/zero`) and CC licence names ("CC BY-NC-SA 4.0", "Creative Commons Attribution-NonCommercial …");
    - one licence found: it sets `license_id` with `license_source = file`, keeps the matched line and its page or locator, and pre-fills the attribution from title, author and licence;
    - none found, or several different ones: no licence is set, and the reason is kept;
    - a reader that fails (encrypted, corrupt or scanned file): no licence is set, and the reason says the file couldn't be read;
    - in every case the source then becomes `proposed`. A source deleted while `detecting` is skipped, as at ingest.
  - **Approval:**
    - the proposal shows "detected in the file" with the licence and the matched line quoted, for the admin to check before approving;
    - without a licence, it shows the reason and **Set licence**. It can't be selected for approval until a licence is set, and the server refuses to approve it.
  - **Invariant 1, refined:** the worker never ingests a `proposed` or `detecting` source. It reads a `detecting` upload only to find its licence, and writes nothing but the licence fields, the matched line, the reason and the status. A source is still ingested only after an admin acts in the app.
  - **Invariant 4 holds:** a source with no licence can't leave `proposed`.
  - **Migration, expand only (ADR-0024):**
    - the status set gains `detecting`, and `license_source` gains `file`;
    - `license_id` and `license_source` become nullable, with a CHECK that both are set unless the status is `detecting` or `proposed`;
    - the source gains the matched line, its locator and the detection reason;
    - existing rows keep their values.
  - **Tickets:**
    - #455 builds the tokens page at `/admin/tokens`;
    - #456 takes the sheet option and the migration;
    - the `ingest` service ticket runs the detection;
    - #461 shows the detected licence and Set licence.

    Each is re-read against this amendment.
- **Amended at #423 (the first ingestion), owner-approved at its plan gate, 2026-11-04.**
  - **A provisional chunk budget.** ADR-0033 left the chunk size to the ingestion ticket's retrieval checks, which need `search_knowledge` (#424), itself blocked by #423. The budget is therefore `KNOWLEDGE_CHUNK_MAX_WORDS = 300` with `KNOWLEDGE_CHUNK_MIN_WORDS = 80` as the size under which a section merges with a sibling. It is counted in words, about 400 tokens, since the app has no tokenizer for the embedding model. Both are settings, and #424's retrieval checks settle them; a paid run there needs the owner's yes.
  - **The worker's queue.** `knowledge_sources` gains `claim_id` and `lease_expires_at` (expand only, ADR-0024). A claim takes an `uploaded` source, or an `ingesting` one whose lease lapsed or that has none, with `FOR UPDATE SKIP LOCKED`; every later write names the claim, so a worker that lost it, or whose source was deleted, writes nothing. A file that raises anything unexpected fails as `ingest_error` instead of crashing the worker on every retry.
  - **Chunk counts on the source.** `knowledge_sources` also gains `chunk_count` and `embedding_model`, written with the chunks, so `/admin/knowledge` never aggregates `knowledge_chunks`. A failed re-ingest keeps the last chunks and their count (knowledge-base.md rec 9).
  - **Gotchas:**
    - Pydantic AI's `Embedder` drops OpenRouter's `usage.cost` and estimates cost from genai-prices instead, which ADR-0018 invariant 6 forbids. ADR-0007's single embeddings adapter calls `POST /api/v1/embeddings` directly through `httpx2`.
    - Postgres's `setweight` takes a `"char"`, which a bound string isn't, so the weights A, B and D are written into the SQL as constants.
    - A failed source's `error` holds a closed reason (`IngestFailure`); the admin page words it, and a free-text reason from #422's rows still shows as stored.
  - **Not here:** licence detection for `detecting` sources is still unassigned. #462's body doesn't list it, though the #454 amendment above gives it to "the `ingest` service ticket".
- **Amended at #458 (staging by PMC ID), owner-approved at its plan gate, 2026-11-04.** The Decision and invariants stand.
  - **A staged article's content hash.** A source staged by PMC ID has no bytes until #463 fetches its JATS, but `content_hash` is NOT NULL and unique (ADR-0011).
    - Until then it holds the SHA-256 of `pmc:<PMCID>`. That is unique per article and can't match a file's hash in practice.
    - #463 replaces it with the JATS file's hash when it stores the file. If that hash already belongs to another source, the article is a duplicate of that source and fails with a reason. It is never silently merged.
    - Rejected: an expand migration making `content_hash` nullable for these sources. It would add a migration and a CHECK just to say "not fetched yet", and the form's `ON CONFLICT (content_hash)` dedupe would have to work around a nullable unique key.
  - **Which version.** PMC keys metadata by article version, as `metadata/<PMCID>.<n>.json`.
    - Staging lists the PMC ID's versions with ListObjectsV2 on the prefix `metadata/<PMCID>.` and reads the highest one.
    - That is two anonymous GETs per ID, both naming only the PMC ID (invariant 7). The JSON's `xml_url` and `pdf_url` are not followed until #463.
    - Source: the [bucket README](https://pmc-oa-opendata.s3.amazonaws.com/README.txt), read 2026-11-04.
  - **The licence code.** The JSON writes CC codes with a space (`CC BY`, `CC BY-NC-ND`, `CC0`), as read for PMC13692514 and a sample of ten articles on 2026-11-04. They map to `license_id` with `license_source = pmc`. `TDM`, a missing code or any other value is refused.
  - **Covered.** An ID is reported covered, without a fetch, when a source has that PMC ID, whatever its status. After the fetch it is also covered when a source has the article's DOI or PMID, such as the same paper uploaded as a PDF.
  - **A request's bound.** A request takes 1 to 20 articles, each PMC ID once, and stages five at a time. `PMC_TIMEOUT_SECONDS` (default 8, at most 10) is one deadline for an article's two GETs together, so a request answers within about 40 seconds, inside Cloudflare's 100.
  - **Gotchas:**
    - An httpx timeout bounds each phase (connect, read, write, pool) separately, so it alone can't bound a request. The adapter wraps both GETs in one `asyncio.timeout`.
    - The integration tests' session factory joins every session to one connection, so they run the per-ID stages one at a time. The app runs five at a time, each on its own pooled connection.
- **Amended at #424 (`search_knowledge`), owner-approved at its plan gate, 2026-11-04.** ADR-0033 left two things to this ticket: how long a search may take, and what it does when the pinned upstream is down. The Decision and invariants stand.
  - **The query instruction is a file.** `llm/prompts/knowledge.query/v1.en.md` holds the `Instruct:` line, followed by `Query:`, in the model card's form; the question is appended as written. A document still carries no instruction (ADR-0033).
    - Its `llm_calls` row is keyed `knowledge.query`, version 1, with the athlete who searched as the acting user (ADR-0018). Only the question leaves the app.
    - No `/tune-prompt` smoke set applies. Smoke sets grade generated text, and the only grader for this line is the retrieval checks below.
  - **Budget.** A query embedding isn't charged to ADR-0029's per-user budget on its own. It runs inside a specialist's tool loop, which already holds one slot and counts once (ADR-0010, #421 amendment).
  - **Time, and the upstream down.** `KNOWLEDGE_QUERY_TIMEOUT_SECONDS` (default 10) bounds the query's embedding call. It runs on its own gateway instance, so ingestion keeps `EMBEDDING_CALL_TIMEOUT_SECONDS`.
    - A timeout, an error, a 429 or a wrong-shaped vector returns `success=False`, with a recovery hint to answer without citing and say no source could be checked.
    - There is **no full-text-only fallback.** It would silently drop the passages in the other language, which only the vector leg finds (hybrid-search.md finding 5).
  - **Fusion.** RRF at k = 60 over two lists, each `KNOWLEDGE_SEARCH_DEPTH` deep (default 20, as pgvector's `rrf.py`; 1–100). `limit` is 1–10, default 5, and `truncated` says when more matched.
  - **The vector leg** uses cosine distance on the HNSW index. Its filters are the embedding model (always), the sport (`= ANY(sports)`), topics (overlap) and `exercise_id`, **never language**.
    - `hnsw.iterative_scan = relaxed_order` is set with `SET LOCAL` in the search's transaction, so a pooled connection never carries it on.
    - The rows are sorted by distance again in code, which stands in for the README's materialized CTE ([pgvector README, Iterative Index Scans](https://github.com/pgvector/pgvector/blob/master/README.md), via context7, 2026-11-04).
  - **The full-text leg is one list.** `websearch_to_tsquery` runs in `english` for English chunks and in `simple` for Polish ones, the configurations their `tsvector` was built with. It takes the same filters and ranks by `ts_rank_cd`. Whether to split it into two RRF inputs stays with the retrieval checks.
  - **The retrieval checks move to their own ticket.** The #423 amendment said #424's checks settle the chunk budget. Those checks need an ingested corpus and paid embedding runs, and no corpus is ingested yet.
    - Until that ticket runs hybrid-search.md finding 6's checks on real questions in both languages, `KNOWLEDGE_CHUNK_*`, the depth, k and the query instruction stay provisional.
    - The checks' paid runs need the owner's yes.
  - **Not on `gym.answer` yet.** The app's toolset offers every server tool to the specialist, so `build_toolset` filters `search_knowledge` out (`HELD_BACK_TOOLS`). #428 lifts the filter when it wires cited answers through `/tune-prompt`. The tool is on the server, so `Client(server)` and the demo reach it.
  - **Demo.** The server has no stdio or HTTP entry, and the MCP Inspector sends no user in the request meta. So the demo is `scripts/search_knowledge.py "<question>" [--sport gym]`, which calls the tool as the seeded dev user with one real embedding call.
  - **Gotchas (#424 review):**
    - A chunk records the configured `EMBEDDING_MODEL`, not the response body's `model`. The `llm_calls` row keeps the billed name. The response names it `Qwen/Qwen3-Embedding-8B` (one real query call, 2026-11-04), which a filter on `qwen/qwen3-embedding-8b` never matches. ⚠ A source ingested before this change records that spelling on its chunks and `knowledge_sources.embedding_model`, so search can't see it until an admin re-ingests it.
    - The vector leg orders by distance alone. A tie-breaker in SQL put an incremental sort over the HNSW scan (`EXPLAIN`, PostgreSQL 18, 2026-11-04), so ties are broken in code.
    - **No relevance floor yet.** The vector leg returns up to `depth` chunks whenever any pass the filters, so `no_match` fires only on an empty or filtered-out corpus, and a question the corpus doesn't cover still gets its nearest passages. A distance floor is for the retrieval checks to set, since no figure exists to set it from.
    - A source whose re-ingest failed keeps its last chunks searchable, as the #423 amendment keeps them (knowledge-base.md rec 9).
- **Amended at #460 (staging a file by presigned PUT), owner-approved at its plan gate, 2026-11-04.** The Decision and invariants stand.
  - **Nothing in the database before register.** `POST /api/admin/knowledge/stage/file` takes the file name and #456's template, validates both, and writes the validated metadata to `uploads` as `staging/<id>.json`. Only an upload a stage issued can register, and no migration is needed. An abandoned stage leaves objects no source names, as ADR-0033 accepts for orphans.
  - **One id, two keys.** The link is a presigned PUT to `staging/<id>.file`, live for 1 hour (inside R2's 1 second to 7 days). The registered source takes that id. Register then puts the bytes it checked under a fresh `sources/<uuid>`, which becomes the source's `storage_key`, and removes the staging objects. Staging again issues a new id and link.
    - **Why a copy.** The link stays live for its whole hour, and a presigned PUT can't be revoked. If it wrote the key a source names, a second PUT after register would swap checked bytes for unchecked ones, which the worker would then ingest (found by `/code-review` at #460).
    - **Why a fresh key per register.** Two registers of one upload each write their own copy. The one whose insert loses removes its own copy, never the winner's.
    - **The cost.** The app sends the file to the bucket once more, at most `KNOWLEDGE_MAX_UPLOAD_MB`, from bytes register already holds. No copy operation is added to `ObjectStoragePort`.
  - **Covered at the stage.** A source with the template's PMC ID, DOI, PMID or ISBN is reported `covered`, and no link is issued. `find_by_identifiers` gains `isbn`, which is unique like the others.
  - **Register** (`POST …/register`, `{upload_id}`) runs ADR-0033's checks on the stored object:
    - it reads the object's size with a HEAD first, so an oversized one is refused unread;
    - then type by content and extension, and the content hash;
    - the size limit is the form's `KNOWLEDGE_MAX_UPLOAD_MB` (owner, at the plan gate). Larger books would need their own setting and ticket, since register reads the file into the web process.
  - **Its outcomes:**
    - `registered`: the source is `proposed`, with the registering admin as `uploaded_by_user_id`. A template without a licence is allowed, since `proposed` may wait for one (#461).
    - `covered`: the same hash or identifiers are stored already.
    - `refused`: the file is empty, too large or of the wrong type.
    - `missing`: nothing was PUT yet, and the stage stands;
    - `expired`: nothing was PUT before the link expired. The stage is removed, so the client stages again.

    `covered` and `refused` delete the new object and its stage. A stage that no longer reads or validates, for instance after a deploy dropped a sport it named, is removed and answers 404, as an unknown upload does.
  - **Only an object no source names is deleted.** A register whose upload is already a source answers `registered` with it, so a retry after a lost answer is safe: its object is under a key no link writes.
  - **Gotchas:**
    - The link can't pin Content-Type or length (`obstore`'s `sign_async` takes no headers, research finding 1), so register checks both on the stored bytes.
    - Under `make start` the link names `storage:7070`. versitygw's port is published on the host's loopback, so the host reaches it with `curl --connect-to storage:7070:127.0.0.1:7070 -T <file> "$PUT_URL"`. That keeps the Host header the link was signed for, so there is no dev-only signing endpoint. In production the link names R2's S3 endpoint.
    - The test S3 server in `tests/object_storage_support.py` checks a PUT link's expiry as it does a GET link's, and answers HEAD.
- **Amended at #427 (the catalogue's texts), owner-approved at its plan gate, 2026-11-05.** ADR-0033 *Catalogue texts* says the worker ingests `exercise_texts` as one chunk per exercise and language, again whenever `import-exercises` changes a text. This records how. The Decision and invariants stand.
  - **Two sources, one per language.** Each is titled "Exercise catalogue":
    - `source_type` and `evidence_tier` are `catalogue`, `sports = {gym}`, and `language` is `en` or `pl`;
    - there is no file, so `storage_key` and the file columns are NULL;
    - `content_hash` is the SHA-256 of `catalogue:<lang>`, unique per language and never a file's hash in practice, as #458 keys staged articles;
    - the licence is `other`/`admin` with the note "MIT licence (data/catalogue/LICENSE.exercises-dataset)", and the attribution is the exercises' own (ADR-0031 G10).

    The admin list shows two rows with their chunk counts, not one per exercise.
  - **Queueing.** `import-exercises` creates any missing source as `uploaded`. When the import inserted, updated or rewrote anything (a renamed exercise changes its chunk's heading), it sets both sources `uploaded` again.
    - That includes an `ingesting` source. Its worker's later writes name a status that is now gone, so they change nothing, and the next claim works out the difference afresh.
    - The output line gains `catalogue_queued=<n>`.
    - Invariant 1 is about uploaded and proposed sources. The catalogue is queued by the owner's `import-exercises` run, as ADR-0033 provides.
  - **The acting user** of the embedding calls (`llm_calls.user_id` is NOT NULL) is the oldest `active` user whose email is in `ADMIN_EMAILS`, compared case-insensitively. It is resolved on every import and stored as the source's `uploaded_by_user_id`. Admin rights still come only from the setting (ADR-0005).
    - With no such user, the source is queued without one and the worker fails it as `no_acting_admin`.
    - An admin's Re-ingest on `/admin/knowledge` then runs it as that admin.
  - **Only what changed is embedded.** The worker reads the language's texts and the source's chunks (no vectors). It embeds an exercise only when:
    - it has no chunk;
    - its chunk's text or heading differs;
    - its chunk was embedded by a model other than the configured `EMBEDDING_MODEL`.

    A run with nothing changed makes no call.
  - **The chunk:**
    - its text is the exercise's `instructions`;
    - its heading path is the exercise name, with a `section` locator of the name and `section_kind = body`;
    - it is embedded as `Exercise catalogue — <name>`, then the text, in this ADR's form;
    - its `tsvector` weights the **exercise name A** and the text D, in `english` or `simple`. The source title would put the same two words in every chunk.
  - **Writes per batch, unlike a file.** Each embedded batch is written at once, under the claim:
    - a replaced chunk keeps its ordinal, and a new exercise takes the next free one;
    - a failed batch fails the source and keeps every chunk written before it, and the next run embeds only the rest;
    - finishing removes the chunk of an exercise with no text left in that language, then sets `ingested`, `chunk_count` and `embedding_model`.

    A file's chunks still go in one write, because they only make sense as a set. A catalogue chunk stands alone.
  - **Cost.** Counted on the worktree's import of commit `7455efa` (2026-11-05): 1,315 English texts (607,098 characters) and 1,314 Polish (573,028).
    - That is about 370,000 tokens with the headings, matching the 376,000 estimated above.
    - DeepInfra charges $0.01/M (OpenRouter's public endpoints API, 2026-11-05; the MCP wasn't authorized), so a full embed is about **$0.004** and a run with nothing changed costs nothing.
  - **Gotcha:** `dict()` over a SQLAlchemy `Result` treats it as a mapping, because a `Result` has `keys()`. Rows are built into a dict with a comprehension.
