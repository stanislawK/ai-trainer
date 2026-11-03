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
- **Amended at #454 (licence detection on form uploads),** owner-directed 2026-11-03 at that ticket's plan gate, on the [Admin Staging mock](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Admin+Staging.dc.html). A file the admin uploads by hand carries no machine-readable licence, and the admin shouldn't have to know one. Papers staged by PMC ID already take theirs from PMC.
  - **The sheet:**
    - the Licence select's first option, and its default, is **Detect from the file**;
    - picking a licence keeps the upload `uploaded` at once, as above;
    - with Detect, the upload is stored as `detecting`, with no licence.
  - **Detection:**
    - the `ingest` container picks `detecting` sources and runs only the file's reader and a deterministic match, with no chunking, no embedding and no model;
    - the match looks for CC licence URLs (`creativecommons.org/licenses/<code>/<version>`, `creativecommons.org/publicdomain/zero`) and CC licence names ("CC BY-NC-SA 4.0", "Creative Commons Attribution-NonCommercial …");
    - one admitted licence found: it sets `license_id` with `license_source = file`, keeps the matched line, and pre-fills the attribution from title, author and licence;
    - none, several different ones, or a refused one: no licence is set, and the reason is kept;
    - either way the source becomes `proposed`.
  - **Approval:**
    - the proposal shows "Detected in the file" with the licence and the matched line quoted, for the admin to check before approving;
    - with no licence, it shows the reason and **Set licence**, and it can't be approved until one is set.
  - **Invariant 1, refined:** the worker never ingests a `proposed` source. It reads a `detecting` upload only to find its licence, and writes nothing but the licence fields, the reason and the status. An upload is still ingested only after an admin acts in the app.
  - **Migration, expand only (ADR-0024):** the status set gains `detecting`, `license_source` gains `file`, and the source gains the matched line and the detection reason.
  - **Tickets:**
    - #456 takes the sheet option and the migration;
    - the `ingest` service ticket runs the detection;
    - #461 shows the detected licence and Set licence.

    Each is re-read against this amendment.
