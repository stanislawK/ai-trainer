# Research — knowledge base ingestion

| | |
|---|---|
| Ticket | #370 |
| Read on | 2026-10-31 |
| Feeds | ADR-0011, ADR-0033; the M3 ingestion, admin upload and `/research-knowledge` tickets |

Scope: ingestion only. Search, fusion and the embedding model belong to [hybrid-search.md](hybrid-search.md) and the embedding spike (ADR-0033).

Reading convention: a version in a package row is the latest release on the project's PyPI page on the read date. Quotes are verbatim. Statements marked *(reasoning)* are this document's own inference, not a sourced claim. ADR statements cite [ADR-0011](../adr/0011-knowledge-base-and-ingestion.md) and [ADR-0033](../adr/0033-knowledge-ingestion-and-admin-upload.md) as of their Proposed text on 2026-10-31.

## Questions

1. Which permissively licensed parsers extract text from PDF, EPUB and transcripts (`.vtt`, `.srt`)?
2. How is a scanned PDF handled (OCR)?
3. How are documents chunked, and at what sizes?
4. How does a chunk carry a citation locator (page, chapter, timestamp)?
5. How should evidence tiers be defined?
6. How are duplicates and re-ingestion handled?
7. How is an in-process ingestion job made resumable?
8. How is text inside an uploaded document kept from acting as instructions?

## Findings

### 1. Parsers and their licences

| Package | Version (PyPI) | Licence | Licence source | Notes |
|---|---|---|---|---|
| pypdf | 6.21.4, 2026-10-28 | BSD-3-Clause | [LICENSE](https://github.com/py-pdf/pypdf/blob/main/LICENSE), read 2026-10-31 | Pure Python; `page.extract_text()`; `page_labels` ([PyPI](https://pypi.org/project/pypdf/), [docs](https://pypdf.readthedocs.io/en/stable/modules/PdfReader.html), read 2026-10-31) |
| pdfminer.six | 20260107 | MIT | [LICENSE](https://github.com/pdfminer/pdfminer.six/blob/master/LICENSE), read 2026-10-31 | Text with positions; "Support for PDF-1.7 specification (well, almost)"; community fork with "limited maintainer availability" ([PyPI](https://pypi.org/project/pdfminer.six/), read 2026-10-31) |
| pdfplumber | 0.11.10, 2026-06-15 | MIT | [LICENSE.txt](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt), read 2026-10-31 | Built on pdfminer.six; tables; "Works best on machine-generated, rather than scanned, PDFs"; no OCR ([PyPI](https://pypi.org/project/pdfplumber/), read 2026-10-31) |
| pypdfium2 | 5.14.0, 2026-10-04 | BSD-3-Clause or Apache-2.0 (PyPI field; the upstream LICENSE URL returned 404) | [PyPI](https://pypi.org/project/pypdfium2/), read 2026-10-31 | PDFium binding; renders pages to images; "PDFium is inherently not thread-safe" |
| Docling | 2.145.0, 2026-10-29 | MIT (code); "For individual model usage, please refer to the model licenses" | [LICENSE](https://github.com/docling-project/docling/blob/main/LICENSE), [PyPI](https://pypi.org/project/docling/), read 2026-10-31 | Parses PDF, EPUB and WebVTT to one document model; OCR extras `easyocr`, `tesserocr`, `rapidocr` |
| unstructured | 0.27.36, 2026-10-30 | Apache-2.0 | [LICENSE.md](https://github.com/Unstructured-IO/unstructured/blob/main/LICENSE.md), read 2026-10-31 | Needs system packages (`poppler-utils`, `tesseract-ocr`, `libmagic-dev`); sends analytics unless `DO_NOT_TRACK` is set ([PyPI](https://pypi.org/project/unstructured/), read 2026-10-31) |
| PyMuPDF | 2.0, 2026-10-19 | **AGPL-3.0** or Artifex commercial: "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License" | [PyPI](https://pypi.org/project/PyMuPDF/), read 2026-10-31 (upstream COPYING returned 404) | Not permissive |
| ebooklib | 0.20, 2025-10-26 | **AGPL-3.0-or-later** | [LICENSE.txt](https://github.com/aerkalov/ebooklib/blob/master/LICENSE.txt), read 2026-10-31 | Not permissive; "EbookLib 0.20 is the final version supporting Python 2.7" |
| pysrt | 1.1.2, 2020-01-20 | **GPLv3** (PyPI field; upstream LICENSE returned 404) | [PyPI](https://pypi.org/project/pysrt/), read 2026-10-31 | Not permissive; last release 2020 |
| srt | 3.5.3, 2023-03-28 | MIT | [LICENSE](https://github.com/cdown/srt/blob/develop/LICENSE), read 2026-10-31 | "No dependencies outside of the standard library" ([PyPI](https://pypi.org/project/srt/)) |
| webvtt-py | 0.6.0, 2026-10-25 | MIT | [LICENSE](https://github.com/glut23/webvtt-py/blob/master/LICENSE), read 2026-10-31 | Reads WebVTT; `webvtt.from_srt()` converts SubRip ([PyPI](https://pypi.org/project/webvtt-py/)) |
| lxml | 6.1.3, 2026-09-02 | BSD-3-Clause | [LICENSE.txt](https://github.com/lxml/lxml/blob/master/LICENSE.txt), read 2026-10-31 | XML/XHTML with XPath; 6.1.3 fixes "External parameter entity parsing was allowed by default" ([PyPI](https://pypi.org/project/lxml/)) |
| selectolax | 1.0.2, 2026-10-22 | MIT (engine Lexbor: Apache-2.0) | [LICENSE](https://github.com/rushter/selectolax/blob/master/LICENSE), read 2026-10-31 | HTML5 parser; XML mode not described |

EPUB structure, from the W3C EPUB 3.3 Recommendation of 13 January 2026 ([spec](https://www.w3.org/TR/epub-33/), read 2026-10-31):
- "The spine element defines an ordered list of manifest item references that represent the default reading order" (§5.7.1).
- The navigation document is mandatory and has exactly one `toc` nav that "defines the primary navigational hierarchy" (§7.1, §7.4.2).
- Reading order and chapter headings are therefore available from the package's own files, with no EPUB-specific library.
- Non-linear spine items exist ("EPUB creators MUST provide a means of accessing all non-linear content", §5.7.2).

Transcript formats:
- WebVTT is a W3C Candidate Recommendation Draft of 20 May 2026, not yet a Recommendation. A file starts with `WEBVTT`; a cue is `00:11.000 --> 00:13.000`, hours optional ([spec](https://www.w3.org/TR/webvtt1/), read 2026-10-31).
- SubRip uses a comma as the decimal separator, e.g. `00:00:33,843 --> 00:00:38,097` ([srt README example](https://pypi.org/project/srt/), read 2026-10-31). No primary specification was found (see Open questions).

### 2. OCR

- Tesseract is Apache-2.0 ([LICENSE](https://github.com/tesseract-ocr/tesseract/blob/main/LICENSE), read 2026-10-31). Release 5.5.4 "fixes 17 security advisories and several smaller issues" ([releases](https://github.com/tesseract-ocr/tesseract/releases/latest), read 2026-10-31; the page shows no year).
- OCRmyPDF 17.13.0 (2026-09-28) is MPL-2.0 ([LICENSE](https://github.com/ocrmypdf/OCRmyPDF/blob/main/LICENSE), [PyPI](https://pypi.org/project/ocrmypdf/), read 2026-10-31). It "adds an OCR text layer to scanned PDF files" and "requires external program installations of Ghostscript and Tesseract OCR".
- The PyPI page of OCRmyPDF links the Tesseract language data for "more than 100 languages" ([PyPI](https://pypi.org/project/ocrmypdf/), read 2026-10-31); the licence of that data was not checked.
- A scanned page gives nothing to a text extractor: PyMuPDF's page says "text extraction on scans returns nothing" ([PyPI](https://pypi.org/project/PyMuPDF/), read 2026-10-31), and pdfplumber works best on machine-generated PDFs (see table).
- Docling offers OCR through optional engines and lists examples such as "Force full page OCR" ([docs](https://docling-project.github.io/docling/), read 2026-10-31). Its defaults were not read.

### 3. Chunking and sizes

- Anthropic: "The choice of chunk size, chunk boundary, and chunk overlap can affect retrieval performance." It gives no recommended size: chunks are "usually no more than a few hundred tokens", and its cost example assumes 800-token chunks ([Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval), 2024-09-19, read 2026-10-31).
- Contextual retrieval prepends a generated context of "usually 50-100 tokens" to each chunk before embedding. Top-20 retrieval failure fell from 5.7% to 3.7% with contextual embeddings and to 2.9% with contextual BM25 added (same source). That evaluation used Gemini Text 004 embeddings and Claude 3 Haiku for the context, on Anthropic's own datasets. It needs one model call per chunk.
- LangChain's recursive splitter measures size in characters by default, with default separators `["\n\n", "\n", " ", ""]`; its docs call it "the recommended one for generic text" ([splitters](https://docs.langchain.com/oss/python/integrations/splitters/index.md), [recursive splitter](https://docs.langchain.com/oss/python/integrations/splitters/recursive_text_splitter), read 2026-10-31). The page does not state default chunk size or overlap.
- LlamaIndex's `MarkdownNodeParser` "splits a document into Nodes using Markdown header-based splitting logic. Each node contains its text content and the path of headers leading to it" ([API reference](https://developers.llamaindex.ai/python/framework-api-reference/node_parsers/sentence_splitter/), read 2026-10-31). A heading path per chunk is a ready-made chapter locator.
- Chen et al. found that indexing by "fine-grained units such as propositions significantly outperforms passage-level units in retrieval tasks" ([arXiv:2312.06648](https://arxiv.org/abs/2312.06648), v3 2024-10-04; the arXiv page names no venue). It compares retrieval units, not sizes and overlaps.
- No peer-reviewed study of chunk size and overlap with a verified venue was found (see Open questions).

### 4. Citation locators

- Anthropic's citations feature indexes PDF pages as "Page numbers are 1-indexed with exclusive end page numbers", plain text by character, and custom content by block ([Citations docs](https://platform.claude.com/docs/en/build-with-claude/citations), read 2026-10-31). Plain-text documents are split into sentences for citing; for transcripts the docs point to custom content documents.
- Citations cannot be combined with structured outputs: it returns a 400 (same source). The ADR-0015 choice card and Pydantic AI structured replies are the places this could matter.
- A PDF can carry printed page labels that differ from physical order. pypdf exposes `page_labels` as "A list of labels for the pages in this document" ([docs](https://pypdf.readthedocs.io/en/stable/modules/PdfReader.html), read 2026-10-31). ISO 32000 was not read.
- EPUB: the spine gives the reading-order file and the navigation document gives the chapter title (see finding 1). The EPUB Canonical Fragment Identifier spec could not be reached (see Open questions).
- Transcripts: a cue start time, e.g. `00:11.000`, is the timestamp locator ([WebVTT](https://www.w3.org/TR/webvtt1/), read 2026-10-31).

### 5. Evidence tiers

- GRADE rates certainty of an effect estimate as High, Moderate, Low or Very Low. "Randomized trials without important limitations provide high quality evidence" and "observational studies without special strengths or important limitations provide low quality evidence" ([GRADE handbook](https://gdt.gradepro.org/app/handbook/handbook.html), updated October 2013, read 2026-10-31).
- The same handbook says "Expert opinion is not a category of quality of evidence", and that being a systematic review is not a quality tier ("GRADE recommends that systematic reviews should form the basis for making health care recommendations").
- GRADE grades evidence for a question and downgrades for risk of bias, inconsistency, indirectness, imprecision and publication bias. It does not grade documents by type (same source). The handbook is being replaced by the GRADE Book; it was not read.
- No primary source on an exercise-science evidence hierarchy was found, and the Oxford CEBM levels page returned 403 (see Open questions).

### 6. Deduplication and re-ingestion

- PostgreSQL 18.6: `INSERT … ON CONFLICT DO UPDATE` "guarantees an atomic INSERT or UPDATE outcome … even under high concurrency"; with `DO NOTHING` the conflict target is optional ([INSERT](https://www.postgresql.org/docs/current/sql-insert.html), read 2026-10-31).
- SHA-256 is defined in [FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final) (August 2015, "hash algorithms that can be used to generate digests of messages"); the landing page does not itself state digest sizes.
- Near duplicates are common in text collections: "existing language modeling datasets contain many near-duplicate examples and long repetitive substrings" (Lee et al., [ACL 2022](https://aclanthology.org/2022.acl-long.577/), pp. 8424-8445, read 2026-10-31). That paper concerns training corpora, not a small curated knowledge base.
- A byte hash identifies a file's bytes only: a re-exported PDF or a second edition of a book has a different hash. *(reasoning, from what a content hash is; no source)*

### 7. Resumable in-process jobs

- PostgreSQL 18.6 `SELECT … FOR UPDATE SKIP LOCKED` skips rows that cannot be locked immediately; the manual says this "can be used to avoid lock contention with multiple consumers accessing a queue-like table" and is "not suitable for general purpose work" ([SELECT](https://www.postgresql.org/docs/current/sql-select.html), read 2026-10-31).
- Celery 5.6 shows the usual queue contract: a message "is not removed from the queue until that message has been acknowledged by a worker", and "Ideally task functions should be idempotent"; `acks_late` suits idempotent tasks ([tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html), read 2026-10-31). It is cited for the idempotency principle only, not as a dependency.
- No Postgres-native durable job-status pattern was found in the official manual.

### 8. Prompt injection from document text

- OWASP's LLM01:2025 defines indirect injection: "Indirect prompt injections occur when an LLM accepts input from external sources, such as websites or files." It says "it is unclear if there are fool-proof methods of prevention" ([OWASP LLM01:2025](https://genai.owasp.org/llmrisk/llm01-prompt-injection/), read 2026-10-31).
- Its mitigations: constrain model behavior; validate output formats "using deterministic code"; input and output filtering; least privilege, handling functions "in code rather than providing them to the model"; human approval for high-risk actions; "Separate and clearly denote untrusted content"; adversarial testing (same source).
- Greshake et al.: "LLM-Integrated Applications blur the line between data and instructions", and attacks work "by strategically injecting prompts into data likely to be retrieved" ([arXiv:2302.12173](https://arxiv.org/abs/2302.12173), v2 2023-05-05; the arXiv page names no venue).
- Extraction XML (EPUB) is also an attack surface for the parser: lxml 6.1.3 fixed external parameter entity parsing being allowed by default (see finding 1).

## Recommendations for this project

Each is checked against ADR-0011 and ADR-0033 (both Proposed). Nothing here edits an ADR.

1. **PDF text:** use pypdf (BSD-3-Clause) first; pdfminer.six or pdfplumber (MIT) when layout or tables matter. Reject PyMuPDF (AGPL). The choice stays with the ingestion ticket. *Findings 1. ADR-0033 "permissively licensed parser (checked at the ticket)"; ADR-0011 defers parsers to ADR-0033: consistent with both.*
2. **EPUB:** read the container with the standard library's `zipfile`, follow the spine for order and the navigation document for chapter titles, parse XHTML with lxml (BSD-3-Clause, at ≥ 6.1.3). Reject ebooklib (AGPL). Docling (MIT code; its model licences are unchecked, see Open questions) is a heavier option that also covers EPUB and WebVTT, not recommended until those are checked. *Findings 1. ADR-0033 lists EPUB; ADR-0011 defers parsers: consistent with both.*
3. **Transcripts:** `srt` (MIT) for `.srt`, `webvtt-py` (MIT) for `.vtt`. Reject pysrt (GPLv3). *Findings 1. ADR-0033 lists `.txt`, `.vtt`, `.srt`; ADR-0011 names timestamps as a locator: consistent with both.*
4. **OCR:** treat scanned PDFs as a separate path: Tesseract (Apache-2.0), run as an external process. OCRmyPDF (MPL-2.0) is a wrapper option only once Ghostscript's licence is checked (see Open questions). Detect "no extractable text" and mark the source `failed` with a clear error rather than ingest an empty source until OCR is built. *Findings 1, 2. **Proposal for `/update-docs`:** ADR-0033 lists steps "extract text … clean, chunk …" but does not mention OCR or what happens to a scanned PDF; ADR-0011 defers OCR to a follow-up. The ingestion ADR text or ticket should state it, including the system packages the image needs (ADR-0023 builds the image).*
5. **Chunking:** split on headings (EPUB navigation, Markdown headers) and fall back to paragraph then sentence boundaries; keep the heading path with the chunk; start near "a few hundred tokens" and settle the number on retrieval checks over the first sources, as ADR-0033 already says. *Findings 3. ADR-0033 "chunk on headings … chunk size chosen at the ingestion ticket"; ADR-0011 stores `knowledge_chunks` with text and locator and defers strategy: consistent with both.*
6. **Contextual chunk prefixes** (Anthropic's technique) improve retrieval on its evaluation but cost a model call per chunk. Do not adopt by default. If a retrieval check shows a need, it is a decision: **Proposal for `/update-docs`:** ADR-0033 accounts only for embedding calls, so it would need a model choice, an `llm_calls` row per call (ADR-0018) and a `TemplateStr` file (ADR-0008). *Finding 3. ADR-0011 fixes the embedding, not the text embedded, so it is unaffected; ADR-0033 is the one that would change.*
7. **Locators:** store a structured locator per chunk: `page` (1-based, plus the printed label when `page_labels` differs), `chapter` (heading path or navigation title), `timestamp` (cue start in seconds). Keep it a data structure; the display string is built when citing. *Findings 4. ADR-0011 "citation locator (page, chapter, timestamp)": consistent. Because Citations cannot be combined with structured outputs, `search_knowledge` returning the locator in its result (ADR-0033) is the route, not the Anthropic Citations API.*
8. **Evidence tiers:** keep ADR-0011's three source-type tiers as a coarse, admin-assigned label shown to the reader. State in docs that it describes the kind of source, not the certainty of any claim. GRADE rates certainty per question, and it excludes expert opinion as a category, so it cannot decide the tier of a coaching book or a podcast. *Findings 5. ADR-0011 and ADR-0033 (tier set at upload, a license note before ingestion, invariant 5): consistent. **Proposal for `/update-docs`:** ADR-0033 ingests exercise texts as `source_type = catalogue`, but ADR-0011's tier list has no tier for them and ADR-0033 invariant 5 requires one before ingestion. The two ADRs disagree here; one of them should name the tier (see Open questions).*
9. **Dedup and re-ingest:** a unique constraint on the content hash and `INSERT … ON CONFLICT DO NOTHING` make a repeat upload a no-op. Re-ingestion replaces a source's chunks inside one transaction so a failed run leaves the old chunks. Treat a new edition as a new source until near-duplicate detection is shown to be needed. *Findings 6. ADR-0011 and ADR-0033 "re-uploading the same file is a no-op": consistent. The one-transaction replace is *(reasoning)*. ADR-0011 "idempotent by content hash": consistent.*
10. **Resumable job:** the status row (`uploaded → ingesting → ingested | failed`) is the queue. Claim a source with `FOR UPDATE SKIP LOCKED`, make every step idempotent, and on startup move `ingesting` rows back to `uploaded`. *Findings 7. ADR-0033 "in-process worker … durable status row … resumable after a restart, as ADR-0028's background reads are": consistent. The startup reset is *(reasoning)*. ADR-0011 names a CLI job and ADR-0033 replaces it with the in-process worker: consistent with ADR-0033, which supersedes that part.*
11. **Injection:** treat every chunk as untrusted content. Put retrieved text in a clearly delimited block of the answering prompt; give the answering model no tools or privileges that a chunk could trigger; keep every data change a draft the user confirms (CLAUDE.md non-negotiable). Ingestion itself calls no model, only the embedding endpoint, so a poisoned document cannot act at ingest. Admin-only upload lowers the risk but does not remove it, because admins upload third-party material. Add a poisoned-document case to the cited-answer smoke set (ADR-0034). *Findings 8. ADR-0011 invariant 1 (answers cite sources) and invariant 2 (no user data in the knowledge base), ADR-0033 invariants 1 and 5, ADR-0010 and ADR-0020: consistent. "Ingestion itself calls no model, only the embedding endpoint" restates ADR-0033's ingestion steps.*
12. **Parsing hygiene:** disable external entity resolution when parsing EPUB XML, keep lxml at ≥ 6.1.3, and enforce `knowledge_max_upload_mb` before parsing. *Findings 1, 8. ADR-0033 names `knowledge_max_upload_mb`; ADR-0011 is silent: consistent with both.*

## Open questions

What the trusted sources didn't settle. None is decided here.

- **EPUB Canonical Fragment Identifier:** the W3C CFI page was a 404 and the EPUB 3.3 text read did not link it. Whether a CFI is a better chapter locator than spine item plus navigation title is open; settle at the ingestion ticket from the current CFI spec.
- **SubRip specification:** no primary spec found; only library examples. The `.srt` parsing edge cases (decimal comma, overlapping cues) are settled by `srt`'s own tests at the ticket.
- **Anthropic's own prompt-injection guidance:** the page moved and the new URL returned 404. Read it through the live Anthropic docs before the injection smoke case is written.
- **Greshake et al.'s venue and mitigations:** the ACM page returned 403 and the arXiv page names no venue. The paper body was not read. Cite as arXiv only until checked.
- **NIST AI 600-1 and AI 100-2 E2025:** their PDFs could not be extracted, so what they say on indirect injection is unverified.
- **Evidence hierarchy for exercise science:** the Oxford CEBM levels page returned 403, and no primary source on a sports-science hierarchy was found. Whether the three tiers need a finer split (systematic review vs single trial) is open.
- **Tier for `catalogue` sources:** ADR-0033 ingests exercise texts as `source_type = catalogue` but ADR-0011's tiers don't cover them.
- **Chunk size and overlap:** no peer-reviewed study with a verified venue was found, and LangChain's and LlamaIndex's defaults were not confirmed on the pages read. Settle on retrieval checks over the first sources (ADR-0033).
- **Near-duplicate detection:** the evidence is for large training corpora. Whether a curated library of tens to hundreds of sources needs it is open; Broder 1997 (MinHash) could not be retrieved.
- **ISO 32000 page labels:** the spec was not read; pypdf's `page_labels` is the only source for them.
- **OCR licences in the stack:** OCRmyPDF "requires external program installations of Ghostscript and Tesseract OCR". Ghostscript's licence was not checked, nor that of the Tesseract language data. Both must be checked before OCR is built.
- **Docling defaults:** the OCR concept page was not fetched, so its default engine and model licences are unchecked.
- **SHA-256 details:** the FIPS 180-4 landing page does not state digest size; read the standard's PDF when the hash column is specified.
- **Postgres-native job pattern:** the manual documents the `SKIP LOCKED` primitive but no durable job-status pattern; recommendation 10 rests on it plus reasoning. Settle with a resume test at the ingestion ticket.
- **Licences read from PyPI only:** the upstream LICENSE URLs for pypdfium2, PyMuPDF and pysrt returned 404, and BeautifulSoup4's LICENSE file was not read. None of the four is recommended, but pypdfium2 is the likeliest renderer for an OCR path, so check its LICENSE files before relying on it.
- **Tesseract release year:** the releases page shows "28 Oct" with no year, so 5.5.4 is dated by day and month only.
- **selectolax XML mode:** not described, so it is not recommended for EPUB XHTML; lxml is.
- **Scale of OCR need:** whether any planned source is a scanned PDF is unknown until the owner's first uploads.

## Sources

| Source | Kind | Version or date read |
|---|---|---|
| [OWASP LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) | Maker's engineering | 2025 edition, read 2026-10-31 |
| [Greshake et al., indirect prompt injection](https://arxiv.org/abs/2302.12173) | Paper (arXiv; venue unverified) | v2 2023-05-05, read 2026-10-31 |
| [GRADE handbook](https://gdt.gradepro.org/app/handbook/handbook.html) | Official docs | Updated October 2013, read 2026-10-31 |
| [Anthropic, Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval) | Maker's engineering | 2024-09-19, read 2026-10-31 |
| [Anthropic Citations docs](https://platform.claude.com/docs/en/build-with-claude/citations) | Official docs | read 2026-10-31 |
| [LangChain splitters](https://docs.langchain.com/oss/python/integrations/splitters/index.md) and [recursive splitter](https://docs.langchain.com/oss/python/integrations/splitters/recursive_text_splitter) | Official docs | read 2026-10-31 |
| [LlamaIndex node parsers](https://developers.llamaindex.ai/python/framework-api-reference/node_parsers/sentence_splitter/) | Official docs | read 2026-10-31 |
| [Chen et al., Dense X Retrieval](https://arxiv.org/abs/2312.06648) | Paper (arXiv; no venue stated) | v3 2024-10-04, read 2026-10-31 |
| [W3C EPUB 3.3](https://www.w3.org/TR/epub-33/) | Spec | Recommendation 2026-01-13, read 2026-10-31 |
| [W3C WebVTT](https://www.w3.org/TR/webvtt1/) | Spec | Candidate Recommendation Draft 2026-05-20, read 2026-10-31 |
| [NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final) | Spec | August 2015, read 2026-10-31 |
| [PostgreSQL INSERT](https://www.postgresql.org/docs/current/sql-insert.html) and [SELECT](https://www.postgresql.org/docs/current/sql-select.html) | Official docs | 18.6, read 2026-10-31 |
| [Lee et al., Deduplicating Training Data Makes Language Models Better](https://aclanthology.org/2022.acl-long.577/) | Peer-reviewed (ACL 2022) | read 2026-10-31 |
| [Celery tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html) | Official docs | 5.6, read 2026-10-31 |
| [pypdf](https://pypi.org/project/pypdf/) · [LICENSE](https://github.com/py-pdf/pypdf/blob/main/LICENSE) · [PdfReader docs](https://pypdf.readthedocs.io/en/stable/modules/PdfReader.html) | Upstream code and docs | 6.21.4, read 2026-10-31 |
| [pdfminer.six](https://pypi.org/project/pdfminer.six/) · [LICENSE](https://github.com/pdfminer/pdfminer.six/blob/master/LICENSE) | Upstream code | 20260107, read 2026-10-31 |
| [pdfplumber](https://pypi.org/project/pdfplumber/) · [LICENSE.txt](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt) | Upstream code | 0.11.10, read 2026-10-31 |
| [pypdfium2](https://pypi.org/project/pypdfium2/) | Upstream package page | 5.14.0, read 2026-10-31 |
| [Docling](https://pypi.org/project/docling/) · [LICENSE](https://github.com/docling-project/docling/blob/main/LICENSE) · [docs](https://docling-project.github.io/docling/) | Upstream code and docs | 2.145.0, read 2026-10-31 |
| [unstructured](https://pypi.org/project/unstructured/) · [LICENSE.md](https://github.com/Unstructured-IO/unstructured/blob/main/LICENSE.md) | Upstream code | 0.27.36, read 2026-10-31 |
| [PyMuPDF](https://pypi.org/project/PyMuPDF/) | Upstream package page | 2.0, read 2026-10-31 |
| [ebooklib](https://pypi.org/project/EbookLib/) · [LICENSE.txt](https://github.com/aerkalov/ebooklib/blob/master/LICENSE.txt) | Upstream code | 0.20, read 2026-10-31 |
| [pysrt](https://pypi.org/project/pysrt/) | Upstream package page | 1.1.2, read 2026-10-31 |
| [srt](https://pypi.org/project/srt/) · [LICENSE](https://github.com/cdown/srt/blob/develop/LICENSE) | Upstream code | 3.5.3, read 2026-10-31 |
| [webvtt-py](https://pypi.org/project/webvtt-py/) · [LICENSE](https://github.com/glut23/webvtt-py/blob/master/LICENSE) | Upstream code | 0.6.0, read 2026-10-31 |
| [lxml](https://pypi.org/project/lxml/) · [LICENSE.txt](https://github.com/lxml/lxml/blob/master/LICENSE.txt) | Upstream code | 6.1.3, read 2026-10-31 |
| [selectolax](https://pypi.org/project/selectolax/) · [LICENSE](https://github.com/rushter/selectolax/blob/master/LICENSE) | Upstream code | 1.0.2, read 2026-10-31 |
| [Tesseract](https://github.com/tesseract-ocr/tesseract/blob/main/LICENSE) · [releases](https://github.com/tesseract-ocr/tesseract/releases/latest) | Upstream code | 5.5.4, read 2026-10-31 |
| [OCRmyPDF](https://pypi.org/project/ocrmypdf/) · [LICENSE](https://github.com/ocrmypdf/OCRmyPDF/blob/main/LICENSE) | Upstream code | 17.13.0, read 2026-10-31 |
