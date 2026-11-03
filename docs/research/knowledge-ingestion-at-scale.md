# Research — knowledge ingestion at scale

| | |
|---|---|
| Ticket | None yet. Owner-directed 2026-11-03, after #422: "it won't work for the thousands of documents we will need to add" |
| Read on | 2026-11-03 |
| Feeds | ADR-0033 (upload, ingestion, chunking), ADR-0010 (MCP over HTTP), ADR-0005 (tokens), ADR-0029 (limits), ADR-0023 (host); #423–#427 |

The admin page from #422 takes one file and its details per upload. The owner wants thousands of sources, mostly for gym, and an AI pipeline for the admin built on `/research-knowledge`. This document lays out how files could reach the knowledge base at that scale, where they could come from, what licences allow, and how the ingestion method should shape preprocessing and chunking. It decides nothing; the owner decides the plan, and the decisions go through `/update-docs`. It builds on [knowledge-base.md](knowledge-base.md) and [hybrid-search.md](hybrid-search.md) and does not repeat them.

## Questions

1. Can the admin page upload many files at once, and how far does that go?
2. Can the owner copy files straight into the bucket, and the app import them from there?
3. Can an agent upload, through an admin API token or an admin MCP server, while a human stays in control?
4. Where can thousands of eligible gym sources come from, and in what format?
5. Which licences allow storing, chunking, embedding and quoting a source, and what must a source record about its licence?
6. Should every input become one intermediate format, described by one source template, before chunking?
7. Which preprocessing helps hybrid search, and how should chunking differ by input type?
8. What does ingesting thousands of sources cost on this host, and where should the worker run?

## Findings

### 1. Many files through the admin page

- `<input type="file" multiple>` lets the user pick several files. `accept` "doesn't validate the types", so the server checks them, as it does now. The page states no limit on count or total size ([MDN, file input](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/input/file), read 2026-11-03).
- `webkitdirectory` picks a whole folder and "is now available in all browsers", Baseline since August 2025. Each `File` carries a `webkitRelativePath`. Its spec is a WICG draft, not a standard ([MDN, webkitdirectory](https://developer.mozilla.org/en-US/docs/Web/API/HTMLInputElement/webkitdirectory), read 2026-11-03).
- Cloudflare caps a request body at "100 MB" on the Free and Pro plans and "200 MB" on Business, answering "413 Request entity too large" above that ([Cloudflare limits](https://developers.cloudflare.com/workers/platform/limits/), read 2026-11-03).
- Its proxy timeouts are "Proxy Read Timeout | 125" seconds and "Proxy Write Timeout | 30" seconds, both answering 524. Only Enterprise can raise the read timeout ([connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/), [error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/), read 2026-11-03). Whether the 30-second write timeout applies per write or to a whole uploaded body isn't stated.
- R2 presigned URLs, as an alternative to sending files through the app:
  - **Methods:** `GET`, `HEAD`, `PUT` and `DELETE`. "`POST` (multipart form uploads via HTML forms) is not currently supported."
  - **Expiry and domain:** links live from 1 second to 7 days and "cannot be used with custom domains".
  - **Content-Type:** a link can pin it, and a mismatch fails with `403 SignatureDoesNotMatch`.
  - **Secrecy:** "Treat presigned URLs as bearer tokens."

  ([R2 presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/), read 2026-11-03.)
- A browser PUT to R2 needs a CORS policy on the bucket. An expired link answers "`403` `ExpiredRequest` without CORS headers", so the page can't read the reason ([R2 CORS](https://developers.cloudflare.com/r2/buckets/cors/), read 2026-11-03).
- `obstore` 0.12.1, the version in `uv.lock`, signs a `PUT` with `sign_async(store, method, paths, expires_in)`. The signature takes no headers, so it can't pin Content-Type or length ([obstore sign](https://developmentseed.org/obstore/latest/api/sign/); the installed `_sign.pyi`).

### 2. Copying into the bucket, then importing

- rclone supports R2 as provider `Cloudflare`. "For R2 tokens with the "Object Read & Write" permission, you may also need to add no_check_bucket = true" ([rclone S3](https://rclone.org/s3/), read 2026-11-03).
- `rclone copy` "Doesn't delete files from the destination" and skips files already there by size and time or MD5; it has `--dry-run` ([rclone copy](https://rclone.org/commands/rclone_copy/), read 2026-11-03). `aws s3 sync` "Recursively copies new and updated files" and deletes only with `--delete` ([AWS CLI](https://docs.aws.amazon.com/cli/latest/reference/s3/sync.html), read 2026-11-03).
- Wrangler "supports uploading files up to 315 MB and only allows one object at a time"; for bulk, Cloudflare points to rclone ([R2 upload objects](https://developers.cloudflare.com/r2/objects/upload-objects/), read 2026-11-03).
- R2 limits:
  - one object up to "5 GiB (single-part)", 5 TiB in parts;
  - listing returns at most 1,000 keys a page;
  - writes to one key are limited to "1 per second".

  ([R2 limits](https://developers.cloudflare.com/r2/platform/limits/), [R2 extensions](https://developers.cloudflare.com/r2/api/s3/extensions/), read 2026-11-03.)
- R2 prices:
  - storage "$0.015 / GB-month" after a free 10 GB;
  - Class A (put, copy, list) "$4.50 / million requests" after a free million;
  - delete and egress are free.

  So 5,000 files of 5 MB cost about $0.23 a month, and their puts and copies use about 1% of the free Class A tier *(arithmetic)* ([R2 pricing](https://developers.cloudflare.com/r2/pricing/), read 2026-11-03).
- R2 implements CopyObject. An R2-only header, `cf-copy-destination-if-none-match` (beta), refuses to overwrite with "412 PreconditionFailed". obstore's `copy_async` copies within one store, and its `S3Store` accepts that header as `copy_if_not_exists` ([R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/), [R2 extensions](https://developers.cloudflare.com/r2/api/s3/extensions/), read 2026-11-03; the installed `_copy.pyi` and `_store/_aws.pyi`).
- R2 API tokens:
  - **Scope:** "Object Read & Write" covers "read, write, and list objects in specific buckets", and a token can be scoped to a set of buckets but not to a prefix. The page doesn't say whether that permission includes delete ([R2 tokens](https://developers.cloudflare.com/r2/api/tokens/), read 2026-11-03).
  - **Temporary credentials:** these can be limited to one bucket plus prefixes, but never beyond their parent token ([temporary credentials](https://developers.cloudflare.com/r2/api/s3/temporary-credentials/), read 2026-11-03).
- R2 event notifications can send `object-create` events to a Cloudflare Queue, which can be consumed "with a consumer Worker or pull over HTTP from outside of Cloudflare Workers" ([event notifications](https://developers.cloudflare.com/r2/buckets/event-notifications/), [pull consumers](https://developers.cloudflare.com/queues/configuration/pull-consumers/), read 2026-11-03). On Workers Free, Queues keep messages for 24 hours ([Queues pricing](https://developers.cloudflare.com/queues/platform/pricing/), read 2026-11-03).
- The R2 S3 endpoint is `<account>.r2.cloudflarestorage.com`, not the app's domain ([AWS CLI on R2](https://developers.cloudflare.com/r2/examples/aws/aws-cli/), read 2026-11-03). So a copy into the bucket passes neither the app's Cloudflare zone nor Caddy.

### 3. An agent uploading: API token or MCP server

**MCP authorization.**
- The current MCP revision is "**2026-07-28**" ([versioning](https://modelcontextprotocol.io/specification/versioning), read 2026-11-03).
- Authorization is "**OPTIONAL**", and an HTTP transport "**SHOULD** conform". When it does:
  - The server "**MUST** implement OAuth 2.0 Protected Resource Metadata (RFC9728)".
  - The server "**MUST** validate that access tokens were issued specifically for them as the intended audience".
  - Tokens travel as `Authorization: Bearer` and "**MUST NOT** be included in the URI query string".
  - Dynamic client registration "is deprecated and retained for backwards compatibility"; Client ID Metadata Documents are preferred.

  ([MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization), read 2026-11-03.)
- "clients and servers **MAY** negotiate their own custom authentication and authorization strategies" ([MCP basic](https://modelcontextprotocol.io/specification/2026-07-28/basic), read 2026-11-03). The security best practices warn against broad scopes such as `admin:*` ([best practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices), read 2026-11-03).

**FastMCP 4.1.4, the version in `uv.lock`.**
- "Authentication applies only to FastMCP's HTTP-based transports" ([authentication, v4.1.4](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/servers/auth/authentication.mdx)).
- It offers four options:
  - **`TokenVerifier`**, for when "you control both the token issuer and your MCP server". A subclass implements `verify_token`. The base class serves no protected-resource metadata, which keeps it outside conformant OAuth (installed `fastmcp/server/auth/auth.py`).
  - **`RemoteAuthProvider`**.
  - **`OAuthProxy`**.
  - **`OAuthProvider`**.
- `StaticTokenVerifier` "should never be used in production" ([token verification, v4.1.4](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/servers/auth/token-verification.mdx)).
- Google sign-in goes through `OAuthProxy`, because "Google doesn't support Dynamic Client Registration". It keeps its tokens in an encrypted file store by default, and the docs show no email allowlist ([Google, v4.1.4](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/integrations/google.mdx)).
- A tool can require a scope with `@mcp.tool(auth=require_scopes(...))`. A failed check hides the tool and answers not-found ([authorization, v4.1.4](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/servers/authorization.mdx)).

**Claude Code as the client.**
- An HTTP server can take a static header, through `claude mcp add --transport http … --header "Authorization: Bearer …"` or `"headers"` in `.mcp.json` with `${VAR}` expansion. It can also go through OAuth with `/mcp`.
- A tool whose `_meta` sets `"anthropic/requiresUserInteraction": true` prompts "on every call, even in `acceptEdits`, `auto`, and `bypassPermissions`".
- Tool output warns above 10,000 tokens and is capped at 25,000 by default.

([Claude Code MCP](https://code.claude.com/docs/en/mcp), read 2026-11-03.)

**Files through MCP.**
- Tool arguments are JSON checked against `inputSchema`. The spec defines no binary or file input, and no size limit; base64 appears only in results ([MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), read 2026-11-03).
- *Reasoning:* a file sent as an argument would be generated by the model token by token, and it would also meet Caddy's 256 KiB cap on `/mcp` (ADR-0029). Files therefore travel outside MCP: a tool returns a presigned PUT, a script uploads the bytes, and a second tool registers the upload.

**API tokens.**
- "API keys should not be used for user authentication. They should only be used for API clients authentication", and tokens in the URL are a listed weakness ([OWASP API2:2023](https://api-security.owasp.org/editions/2023/en/0xa2-broken-authentication), read 2026-11-03).
- Reference tokens are "generated using a CSPRNG and possess at least 128 bits of entropy" (ASVS 7.2.3). Sessions have an absolute lifetime (7.3.2) and end when the account is disabled (7.4.2) ([ASVS 5.0.0, V7](https://github.com/OWASP/ASVS/blob/v5.0.0/5.0/en/0x16-V7-Session-Management.md)).
- "Do not rely exclusively on API keys to protect sensitive, critical or high-value resources" ([OWASP REST Security](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html), read 2026-11-03). Secrets expire, rotate, are least-privilege, and their use is audited ([OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html), read 2026-11-03).
- No OWASP page read here says to store only a token's hash. Doing so is *reasoning*, by analogy with passwords.

**Bot Fight Mode.**
- It "may challenge API or mobile app traffic". Skip, Bypass and Allow rules "have no effect", and only an IP Access rule exempts a client ([Bot Fight Mode](https://developers.cloudflare.com/bots/get-started/bot-fight-mode/), read 2026-11-03).
- It is on in production (`deploy/RUNBOOK.md` §11.7) and already challenges `/health` probes. So **any non-browser client on the app's domain, API token or MCP alike, may be challenged.**

**A human in the loop.**
- "there **SHOULD** always be a human in the loop with the ability to deny tool invocations". Servers "**MUST** … Implement proper access controls, Rate limit tool invocations" ([MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), read 2026-11-03).
- OWASP: "require a human to approve high-impact actions" and "Implement authorization in downstream systems rather than relying on an LLM" ([LLM06:2025](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/), read 2026-11-03).
- In Claude Code's auto mode, "a separate classifier model reviews actions instead of you" ([Claude Code security](https://code.claude.com/docs/en/security), read 2026-11-03). So a client's prompt is not a human gate the app can rely on *(reasoning)*.
- Injection: "Indirect prompt injections occur when an LLM accepts input from external sources, such as websites or files" ([OWASP LLM01:2025](https://genai.owasp.org/llmrisk/llm01-prompt-injection/), read 2026-11-03). "Servers that fetch external content can expose you to prompt injection risk" ([Claude Code MCP](https://code.claude.com/docs/en/mcp)). An agent that reads third-party papers while holding a write token is that case.

### 4. Where thousands of sources can come from

- **PMC Cloud Service.** The old routes are gone: "the PMC OA Web Service is no longer available", and the legacy FTP and AWS files went away from 24 August 2026 ([OA service](https://pmc.ncbi.nlm.nih.gov/tools/oa-service/), [new in PMC](https://pmc.ncbi.nlm.nih.gov/about/new-in-pmc/), read 2026-11-03). The Cloud Service replaces them:
  - It is the S3 bucket `pmc-oa-opendata`, "world-readable", with no login ([PMC on AWS](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/), read 2026-11-03).
  - It holds about 8 million article versions. Each has JATS XML, plain text and a JSON metadata file, plus the PDF where the licence allows.
  - The JSON carries `license_code` (a Creative Commons code, or `TDM` for author manuscripts available only for text mining), `is_retracted`, `doi` and `xml_url`.
  - Users must "acknowledge NLM as the source of the data in a clear and conspicuous manner".

  ([bucket README](https://pmc-oa-opendata.s3.amazonaws.com/README.txt), read 2026-11-03.)
- **PMC retrieval terms.** "Systematic retrieval (or bulk retrieval) of articles through any other automated process is prohibited" outside the Cloud Service, OAI-PMH, E-Utilities and BioC. "License terms vary" ([PMC OA subset](https://pmc.ncbi.nlm.nih.gov/tools/openftlist/), read 2026-11-03). Users "are directly and solely responsible for compliance with copyright restrictions" ([PMC copyright](https://pmc.ncbi.nlm.nih.gov/about/copyright/), read 2026-11-03).
- **Europe PMC.** It labels articles "made available under a Creative Commons license, or similar", and "The license terms are not identical for all articles in this subset" ([Europe PMC OA](https://europepmc.org/downloads/openaccess), read 2026-11-03). Its REST API:
  - searches on `OPEN_ACCESS`, `LICENSE` and `HAS_FT`, and returns `license` per article ([fields](https://www.ebi.ac.uk/europepmc/webservices/rest/fields?format=json), queried 2026-11-03);
  - returns JATS from `/{PMCID}/fullTextXML` for open-access articles; three non-OA articles answered HTTP 500.

  Its documentation pages answered 403 to the fetcher, so its rate limits and terms are unread (Open questions).
- **OpenAlex.**
  - **Rate limits:** 10 requests a second with no key, 30 with a free key, and "To use the API at any real scale, though, you'll want a free API key" ([rate limits](https://help.openalex.org/how-to-use-the-api/rate-limits-and-authentication), read 2026-11-03).
  - **Licence field:** `license` is "a normalized short code" per location, or null. "Locations always point at someone else's copy" ([location object](https://help.openalex.org/api-entities/works/work-object/location-object), read 2026-11-03).
  - **What counts as open:** OpenAlex counts CC BY-NC-ND as open, and `implied-oa` marks articles with no identifiable licence ([OA licence](https://help.openalex.org/hc/en-us/articles/41193820509079-What-is-an-OA-license), read 2026-11-03).
  - **Unpaywall** now runs on the same database ([Unpaywall](https://help.openalex.org/access/unpaywall/), read 2026-11-03).
- **JATS** is "NISO z39.96-2024 (JATS 1.4; Current Standard)" ([JATS](https://jats.nlm.nih.gov/), read 2026-11-03).
  - `<sec>` is "the basic structural unit of the body of a document", recursive, with an optional `<title>` and a `sec-type` such as `methods` ([sec](https://jats.nlm.nih.gov/archiving/tag-library/1.4/element/sec.html)).
  - `<license>` holds a URI to the licence, per the NISO ALI recommendation ([license](https://jats.nlm.nih.gov/archiving/tag-library/1.4/element/license.html)).
  - A sampled PMC article (PMC13692514) carried `<ali:license_ref specific-use="textmining">https://creativecommons.org/licenses/by/4.0/</ali:license_ref>`, numbered section titles and 62 references.
- **Books and podcasts.**
  - DOAB's metadata is CC0 and harvestable over OAI-PMH, but "DOAB does not host content" ([DOAB FAQ](https://doabooks.org/en/researchers/faq), read 2026-11-03). A live search for "strength training" returned 100 records, many of them reprints of journal special issues, and most with no per-book licence field.
  - No lawful bulk source of commercial coaching books was found.
  - Podcasting 2.0 defines `<podcast:transcript>` and an optional `<podcast:license>` ([transcript](https://podcasting2.org/docs/podcast-namespace/tags/transcript), [license](https://podcasting2.org/docs/podcast-namespace/tags/license), read 2026-11-03). A published transcript is not a licence.
- **Volume.** These are unauthenticated Europe PMC searches, run 2026-11-03, that count full-text matches:

  | Query | All | Open access | OA, CC BY or CC0 | …of which reviews, SRs, MAs |
  |---|---|---|---|---|
  | `"resistance training"` | 59,206 | 36,122 | 25,985 | 9,360 |
  | `hypertrophy AND ("resistance training" OR "strength training")` | 12,456 | 9,001 | 6,807 | 2,869 |
  | `deload OR deloading` | 256 | 194 | 143 | 34 |
  | `"strength training" OR "resistance training" OR "resistance exercise" OR "weight training"` | 98,331 | 61,917 | 43,872 | 13,718 |

  - **Title and abstract only:** limiting the last query to them gives 7,353 open-access articles under CC BY or CC0.
  - **Licence mix:** in a sample of 1,000 open-access "resistance training" records, 77% were CC BY, 13% CC BY-NC-ND, 7% CC BY-NC, and the rest ND, NC-SA or SA.
  - **OpenAlex:** it finds more, at 98,454 works whose best open copy is CC BY, CC0 or public domain. Its search is looser, though, and a licence there doesn't promise full text the project can fetch.

### 5. Licences

- CC BY 4.0:
  - Where an exception already covers a use, "this Public License does not apply" (s. 2(a)(2)).
  - Technical modifications are allowed (s. 2(a)(4)).
  - Attribution means the creator, the copyright notice, a licence reference, a link and an indication of changes, "in any reasonable manner based on the medium, means, and context" (s. 3(a)).

  ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/legalcode.en), read 2026-11-03.)
- "NonCommercial means not primarily intended for or directed towards commercial advantage or monetary compensation." ND forbids sharing adapted material ([CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode.en), read 2026-11-03).
- CC's own guidance:
  - "All six of the 4.0 licenses allow for text and data mining".
  - An excerpt "small enough to be uncopyrightable" is outside the licence, but many small excerpts can add up to "a significant copyrightable chunk".
  - NC "does not turn on the type of user".

  ([CC FAQ](https://creativecommons.org/faq/), read 2026-11-03.)
- On AI use, CC says:
  - "The attribution (BY) condition, the ShareAlike (SA) condition, and the NoDerivatives (ND) restriction are all triggered only upon public sharing."
  - "The NonCommercial (NC) restriction applies to all uses requiring permission under copyright."
  - For retrieval, "providing attribution to the CC-licensed work tied to the particular model output with a link to the source is ideal."

  ([CC on AI training](https://creativecommons.org/using-cc-licensed-works-for-ai-training/), © 2025, read 2026-11-03.)
- EU text and data mining, Directive (EU) 2019/790:
  - **Art. 3** covers only "research organisations and cultural heritage institutions".
  - **Art. 4** allows "reproductions and extractions of lawfully accessible works … for the purposes of text and data mining" unless the use was "expressly reserved", by "machine-readable means" for content online. Copies may be kept "as long as is necessary".

  ([CELEX 32019L0790](https://publications.europa.eu/resource/celex/32019L0790), read 2026-11-03.) *Reasoning, not legal advice:* Art. 4 covers copying, not showing a passage to a user, so it doesn't replace a licence for quoted answers.
- The quotation exception allows quotations "for purposes such as criticism or review", with the source and author named, "in accordance with fair practice" ([Directive 2001/29/EC Art. 5(3)(d)](https://publications.europa.eu/resource/celex/32001L0029), read 2026-11-03).
- Poland implemented Art. 4 in the act of 26 July 2024, Dz.U. 2024 poz. 1254:
  - Art. 26³ ust. 1: "Wolno zwielokrotniać rozpowszechnione utwory w celu eksploracji tekstów i danych, chyba że uprawniony zastrzegł inaczej."
  - Ust. 2: a reservation for online content is made in a machine-readable format.

  ([Dz.U. 2024 poz. 1254](https://api.sejm.gov.pl/eli/acts/DU/2024/1254/text.html), read 2026-11-03.) Polish quotation is art. 29, "w zakresie uzasadnionym celami cytatu" ([Dz.U. 2022 poz. 2509](https://api.sejm.gov.pl/eli/acts/DU/2022/2509/text.html), read 2026-11-03).
- The EU AI Act binds providers of general-purpose models to Art. 4(3) reservations (Regulation (EU) 2024/1689 Art. 53(1)(c), [CELEX 32024R1689](https://publications.europa.eu/resource/celex/32024R1689), read 2026-11-03). *Reasoning:* this project is not such a provider.

### 6. One intermediate format and a source template

- **Docling 2.146.0** (2026-11-03) is MIT-licensed and supports Python 3.14 ([PyPI](https://pypi.org/pypi/docling/json), read 2026-11-03).
  - **Inputs:** it reads PDF, EPUB, Markdown, HTML, WebVTT and JATS XML into one `DoclingDocument`, but not SRT ([supported formats, v2.141.0](https://github.com/docling-project/docling/blob/v2.141.0/docs/usage/supported_formats.md)).
  - **What its readers keep:** the WebVTT reader keeps each cue's start and end time, the EPUB reader follows the spine, and the JATS reader keeps authors, structured abstracts and references ([webvtt_backend.py](https://raw.githubusercontent.com/docling-project/docling/main/docling/backend/webvtt_backend.py), [epub_backend.py](https://raw.githubusercontent.com/docling-project/docling/main/docling/backend/epub_backend.py), [jats_backend.py](https://raw.githubusercontent.com/docling-project/docling/main/docling/backend/xml/jats_backend.py), read 2026-11-03).
  - **Page numbers and headings reach each chunk.** Every item's provenance carries `page_no`, and each chunk's metadata carries its heading path. `HybridChunker` "splits chunks only when needed (i.e. oversized w.r.t. tokens)" and merges "undersized successive chunks with same headings & captions" ([chunking, v2.141.0](https://github.com/docling-project/docling/blob/v2.141.0/docs/concepts/chunking.md)).
- **Docling without torch.** The full `docling` pulls in torch. `docling-slim` offers extras without it: `format-xml-jats`, `format-markdown`, `format-html`, `format-pdf`, and chunking through `docling-core[chunking]`, which needs `transformers` but not torch ([docling-slim](https://pypi.org/pypi/docling-slim/json), [docling-core](https://pypi.org/pypi/docling-core/json), read 2026-11-03).
- **Docling's model licences** (an open question in [knowledge-base.md](knowledge-base.md)) are partly settled:
  - The default layout model is Apache-2.0 ([docling-layout-heron](https://huggingface.co/docling-project/docling-layout-heron), read 2026-11-03).
  - `docling-models` lists both CDLA-Permissive-2.0 and Apache-2.0 without saying which model is which ([README](https://huggingface.co/docling-project/docling-models/raw/main/README.md), read 2026-11-03). Both are permissive.
  - RapidOCR's weights are unchecked.
- **Other converters, all ruled out:**
  - **Unstructured 0.27.36** requires Python `<3.14` ([PyPI](https://pypi.org/pypi/unstructured/json), read 2026-11-03), so it can't be installed here.
  - **pymupdf4llm** is AGPL-3.0 ([LICENSE](https://raw.githubusercontent.com/pymupdf/pymupdf4llm/main/LICENSE), read 2026-11-03).
  - **marker 2.1.0** is now Apache-2.0 in code, but its weights are "free for research, personal use, and startups under $5M funding/revenue" ([marker](https://github.com/datalab-to/marker), [MODEL_LICENSE](https://raw.githubusercontent.com/datalab-to/marker/master/MODEL_LICENSE), read 2026-11-03). Not permissive.
- **Metadata formats for a template:**
  - **CSL-JSON** (schema v1.0, MIT) describes a work with `type`, `title`, `author`, `issued`, `DOI`, `PMID`, `PMCID`, `ISBN`, `abstract`, `keyword`, `language` and `URL`, and its README says the schema "is not yet fully normative" ([csl-data.json](https://raw.githubusercontent.com/citation-style-language/schema/master/schemas/input/csl-data.json), read 2026-11-03).
  - **Dublin Core** has a Rights element: "Information about rights held in and over the resource" ([DCMI terms](https://www.dublincore.org/specifications/dublin-core/dcmi-terms/), read 2026-11-03).
  - **Pandoc YAML front matter** opens and closes with `---` ([Pandoc manual](https://pandoc.org/MANUAL.html), read 2026-11-03).

### 7. Preprocessing and chunking by input type

- **Anthropic's Contextual Retrieval** prefixes each chunk with model-written context. Top-20 retrieval failures fell by 35% with contextual embeddings, by 49% with contextual BM25 added, and by 67% with reranking added. It costs "$1.02 per million document tokens" with prompt caching, measured with Claude 3 Haiku. The post also says "Always run evals" ([Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval), 2024-09-19, read 2026-11-03).
- **Late chunking** is a preprint (arXiv:2409.04701 v3, no venue). It splits "after the transformer and before mean pooling", which needs token-level outputs. OpenRouter's embeddings API returns one pooled vector per input ([arXiv](https://arxiv.org/abs/2409.04701); [OpenRouter embeddings](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request.md), read 2026-11-03). *Reasoning:* late chunking can't run through this project's gateway.
- **Qwen3-Embedding-8B** reads up to 32k tokens. "not using an `instruct` on the query side can lead to a drop in retrieval performance by approximately 1% to 5%", and documents need no instruction ([model card](https://huggingface.co/Qwen/Qwen3-Embedding-8B), read 2026-11-03).
- **Weighting the title and headings in full-text search.** PostgreSQL's `setweight` labels parts of a `tsvector` A to D, "allowing words from different parts of a document to be weighted differently by ranking functions" ([PostgreSQL 18, text search features](https://www.postgresql.org/docs/18/textsearch-features.html), read 2026-11-03). That ranks the title and heading path higher without adding them to the chunk's text.
- **Semantic chunking:** "the computational costs associated with semantic chunking are not justified" against fixed-size chunking (Qu, Tu, Bao, *Findings of NAACL 2025*, pp. 2155–2177, [ACL Anthology](https://aclanthology.org/2025.findings-naacl.114/)).
- **Proposition indexing** beats passages for dense retrieval (Chen et al., *EMNLP 2024*, pp. 15159–15177, [ACL Anthology](https://aclanthology.org/2024.emnlp-main.845/)). This confirms the venue that [knowledge-base.md](knowledge-base.md) left open. Making propositions needs a model call per passage.
- **Tables:** Unstructured keeps each table as its own chunk ([chunking docs](https://docs.unstructured.io/open-source/core-functionality/chunking), read 2026-11-03). Docling writes tables as column–value pairs and can widen a partial table chunk to the whole table ([chunking, v2.141.0](https://github.com/docling-project/docling/blob/v2.141.0/docs/concepts/chunking.md)).
- No peer-reviewed source read here compares an abstract as its own chunk with a document-level vector, or gives chunk sizes for scientific papers.

### 8. Load on this host

- **The host and its limits.** Production is one Hetzner CX23 with 2 vCPU and 4 GB (ADR-0023). `compose.prod.yaml` caps the app and the database at 1 GB each. PostgreSQL's `maintenance_work_mem` defaults to 64 MB ([PostgreSQL 18, resource settings](https://www.postgresql.org/docs/18/runtime-config-resource.html), read 2026-11-03).
- **Docling throughput, from its technical report** (arXiv:2408.09869, preprint): 0.60 to 0.94 pages a second on 4 threads with OCR off, at 2.4 to 6.2 GB peak memory. With EasyOCR on CPU, it reports "upwards of 30 seconds per page" ([report](https://arxiv.org/html/2408.09869v5), read 2026-11-03).
- **Docling throughput, from a later benchmark** (an AAAI 2025 workshop paper, venue from arXiv's comment): about 3.1 seconds a page on 8 x86 threads across 4,008 pages, and EasyOCR about 13 seconds a page ([arXiv:2501.17887](https://arxiv.org/abs/2501.17887), read 2026-11-03).
- **Tesseract** gives no throughput figure. For bulk work it advises "running one Tesseract process per CPU core" with `OMP_THREAD_LIMIT=1` ([tessdoc FAQ](https://tesseract-ocr.github.io/tessdoc/FAQ.html), read 2026-11-03).
- **Building the vector index (pgvector).**
  - "it's faster to create an index after loading your initial data", and "Indexes build significantly faster when the graph fits into `maintenance_work_mem`".
  - A vector takes `4 * dimensions + 8` bytes.
  - An index can be built on an `embedding::halfvec(n)` expression.

  ([pgvector README](https://raw.githubusercontent.com/pgvector/pgvector/master/README.md), read 2026-11-03.)
- **OpenRouter embeddings** take an array of inputs, with no maximum array size documented. Paid models have "different rate limits for different models", and a 429 asks for exponential backoff ([embeddings](https://openrouter.ai/docs/api/reference/embeddings), [limits](https://openrouter.ai/docs/api/reference/limits), read 2026-11-03).
- **Postgres-backed queues.** Both are MIT-licensed and support Python 3.14 (PyPI and upstream CI, read 2026-11-03):
  - **procrastinate 3.10.0** (2026-09-23) documents a worker inside a FastAPI lifespan, a `concurrency` setting, and retry of stalled jobs through a heartbeat. It keeps its own schema.
  - **pgqueuer 1.6.0** (2026-10-11).

## Recommendations for this project

Proposals for the owner. Nothing here edits an ADR; each change goes through `/update-docs`.

1. **Get papers by identifier, not by file.** For research papers, the bulk of any gym corpus, nobody needs to carry files at all (findings 4, 6):
   - **Discovery:** Europe PMC's search is the discovery layer.
   - **Fetching:** the PMC Cloud bucket returns each article's JATS and its licence by PMCID, through a route PMC permits.
   - **The new path:** an *import by identifier*. The admin, or an agent, submits PMCIDs with tier, sports and topics; the worker fetches the JATS and JSON, takes the licence from `license_code`, and refuses anything retracted or under a licence not on the admit list.
   - **Network:** it is an outbound fetch that carries no user data, which fits the CLAUDE.md rule on external lookups. It does, though, open a new outbound network path from production that ADR-0033 must name.
   - **Scale:** this covers roughly 7,000 to 44,000 eligible gym articles.
2. **For files the owner holds (books, PDFs, transcripts), copy them into R2 and import from there** (path 2, finding 2):
   - **Copy:** the owner copies with `rclone copy` into a separate `staging` bucket, under a token scoped to that bucket alone. Prefixes can't be scoped, and it isn't documented whether that token can delete.
   - **Manifest:** a manifest travels with the files (recommendation 6).
   - **Import:** an **Import** button on `/admin/knowledge` validates the whole manifest, reports problems per row, then copies each file into `uploads` and records it as a source. Nothing large passes Cloudflare's proxy, Caddy or Bot Fight Mode, and it resumes for free.
   - **Event notifications aren't needed:** an import the owner starts doesn't need them, and they would add Queues and a second token.
3. **Keep the single-file form for one-offs** (finding 1). A browser bulk upload through the app (path 1) works for tens of files. For thousands it is fragile: a closed tab stops it, every file crosses the 100 MB cap and the 30-second write timeout, and a 5,000-row manifest exceeds Caddy's 256 KiB. Presigned PUTs from the browser would fix the size limits but need bucket CORS, a two-step upload and a separate signing endpoint for `make start`.
4. **For the agent pipeline, prefer an admin API token that can only stage, over an MCP server** (finding 3). Defer the MCP server until external clients need one anyway.
   - **The token:** 32 random bytes, stored as a SHA-256 hash with a prefix shown to the admin, a 30-day expiry, revocation and `last_used_at`. The admin creates it on `/admin` and sees it once. Every request re-checks that its owner is active and still in `ADMIN_EMAILS` (ADR-0005 invariant 8).
   - **Its only scope is staging**, through two JSON routes:
     - *stage* takes the metadata and returns a presigned PUT, or takes PMCIDs (recommendation 1);
     - *register* checks the uploaded object's type, size and hash.
   - **No delete, re-ingest or approval.** A script in the skill moves the bytes, so files never pass through the model.
   - **Human gate in the app.** Every staged source lands as **`proposed`**, which the worker never picks up. The admin approves proposals in bulk on `/admin/knowledge`, after checking tier, licence and title. That keeps ADR-0010's "drafts only" and moves ADR-0033's "Agents don't upload" to "agents only propose".
   - **The skill:** a new `/stage-knowledge` takes a shortlist from `/research-knowledge` that the owner approved, fills the template from the sources' own metadata, stages what is open-access, and lists what the owner must get by hand.
   - **Two blockers first:**
     - Bot Fight Mode may challenge the script on the app's domain, so the owner tests it from their own connection first (Open questions).
     - The budget of model calls per user per hour (ADR-0029) counts by session today, and has to say how a token's calls count.
5. **An admin MCP server later, if wanted**, uses the same token table behind a FastMCP `TokenVerifier`, on a **separate** server from the specialists' tools. That needs ADR-0010's deferred token amendment. A custom verifier isn't conformant MCP OAuth, since it serves no protected-resource metadata. The conformant route is Google through `OAuthProxy`, which needs its own email allowlist, a signing key, persistent storage and a consent screen. Mark its write tools `anthropic/requiresUserInteraction`, but don't rely on that as the gate.
6. **One source template for every path, CSL-JSON plus the project's fields** (finding 6). Each manifest row, or a sidecar file, is a CSL-JSON item (`type`, `title`, `author`, `issued`, `DOI`/`PMID`/`PMCID`/`ISBN`, `abstract`, `keyword`, `language`, `URL`, `container-title`) plus:
   - `evidence_tier`, `source_type`, `sports`, `topics`;
   - a structured licence (recommendation 7).

   JSON Lines keeps it streamable. A JATS article fills most fields itself, and the row only adds tier, sports and topics. The form, the import, the token routes and the skill all validate the same model, the one `read_details` checks today.
7. **Licences: what to admit and what to record** (finding 5; reasoning, not legal advice).
   - **Admit by default:** CC0 and CC BY.
   - **Admit with a flag until a lawyer confirms:** CC BY-SA, since ShareAlike binds shared adaptations and verbatim, attributed quotes plausibly aren't.
   - **Hold for review:** CC BY-ND, about 1% of articles.
   - **NC licences (about 20–27%) depend on whether the app is commercial.**
   - **Refuse:** `TDM`, `implied-oa`, a null licence, or "free to read".
   - **Replace the free-text license note with structured fields:**
     - an SPDX-style licence ID and URI;
     - where the licence came from;
     - the verbatim copyright statement;
     - an attribution string;
     - the retrieval date;
     - for PMC, "Source: NLM PMC".

     Then removing a whole licence class is one query.
8. **Convert every input to one internal document model before chunking** (finding 6). It holds ordered blocks (heading, paragraph, list, table, caption, cue), each with its heading path and an anchor (page and page label, spine item, section, cue time), with metadata kept apart.
   - **Prefer inputs** in this order: JATS, then EPUB, then a born-digital PDF, with OCR last. Never ingest a paper from its PDF when its JATS exists.
   - **Use `docling-slim`'s torch-free readers and `HybridChunker`**, or a small in-house model over pypdf, lxml and webvtt-py, decided by a trial.
   - **Full Docling's PDF path** (torch, 2.4–6 GB) doesn't fit the 1 GB app container. If needed, it runs off the request path, or off the host.
   - **File types:** ADR-0033's list gains `jats` (`.xml`).
9. **Enrich deterministically first, then measure** (finding 7).
   - **Embed with context:** embed `title — heading path` before each chunk's text.
   - **Weight the full-text index:** build the `tsvector` as `setweight(title,'A') || setweight(headings,'B') || setweight(body,'D')`.
   - **Shape the chunks:**
     - make the abstract its own chunk;
     - keep a table whole with its caption;
     - drop running headers and reference lists from what is embedded, keeping references as metadata.
   - **Add the query instruction** Qwen3's card asks for.
   - **Measure** with [hybrid-search.md](hybrid-search.md)'s retrieval checks (R@10 and MRR, English and Polish) across three arms: plain chunks, heading prefix, and heading prefix plus weighted `tsvector`.
   - **Only if that falls short, consider Contextual Retrieval.** It is a model call per chunk, so it needs a template file, an `llm_calls` row, a model choice and `/update-docs`. At Anthropic's figure, about $1 per million document tokens, it would cost roughly $50 for 5,000 papers.
   - **Ruled out:** late chunking can't run through the gateway. Proposition indexing and semantic chunking cost more than the evidence supports.
10. **Chunk by structure, with one token budget** (finding 7):
    - **JATS:** split on `<sec>` and its title; locate by section title and DOI, since JATS has no pages.
    - **EPUB:** spine plus navigation, then headings.
    - **Markdown:** headings.
    - **Born-digital PDF:** paragraphs within a page; locate by page and page label.
    - **VTT and SRT:** cue windows that end on sentences, with the first cue's start time.
    - **Sizes:** one budget, settled by the retrieval checks: merge small siblings under one heading and split only what is too large.
11. **Run ingestion in its own container, from the same image** (finding 8). It runs as an `ingest` service in `compose.prod.yaml`, with its own memory and CPU limits and a concurrency of 1, so chat (PRD G14) never competes with parsing. It is still the app's code running on the host, so ADR-0023's rule on agents is untouched.
    - **Queue:** keep the status row with `FOR UPDATE SKIP LOCKED` and a lease with a heartbeat (knowledge-base.md rec 10). procrastinate is the ready-made alternative, once its separate schema fits Alembic and squawk (ADR-0024).
    - **Budget:** batch embeddings at a few dozen inputs or a token budget per call, with backoff on 429, and one `llm_calls` row per batch. The batches have to fit ADR-0029's per-user budget, since the admin is the acting user.
    - **Backfill:** for a bulk backfill, build the HNSW index after the load with a raised `maintenance_work_mem`.
    - **Scale:** a rough size at 125,000 chunks of 1,024 dimensions is about 0.5 GB of vectors before the index (*arithmetic*), against a database container capped at 1 GB. Either the cap grows, or `halfvec` indexing is considered (hybrid-search.md finding 2), or the host grows.

### What #423 should take from this before it starts

#423 is the first ingestion ticket. Its plan should either wait for the owner's decisions below or be shaped to survive them:
- **The worker:** a worker that can run outside the app process.
- **The chunk schema:** a `heading_path`, a `section_kind`, and a locator that holds a page and page label, a spine item, a section or a cue time.
- **The pipeline:** convert to the internal document model first, then one chunker, with idempotent stages.
- **Embeddings:** batched, under the per-user budget.
- **The index:** built after a bulk load.
- **A trial before the PDF path is fixed:** about 50 real PDFs timed on the CX23.

`jats` as a file type and structured licence fields change #422's table, so they belong to an expand migration in whichever ticket brings them in.

### Decisions for the owner

1. **Which paths to build, and in what order.** Import by PMCID, the bucket import, the staging token with the skill, browser bulk, and the MCP server later.
2. **Whether agents may propose sources.** That changes ADR-0033's "Agents don't upload" to "agents propose, the admin approves".
3. **The commercial status of the app.** That decides whether NC licences are admitted. Then the treatment of SA and ND, ideally with a lawyer.
4. **Ingestion in a sibling container**, and the database memory needed for the target corpus size.
5. **Docling slim or an in-house document model**, decided by a trial on real sources.
6. **Whether model-made enrichment is ever wanted**, decided only after the deterministic arms are measured.

## Open questions

- **Europe PMC's terms and rate limits.** Its developer and copyright pages answered 403 to the fetcher. Read them in a browser before building against its API.
- **Bot Fight Mode on a residential connection.** Does it challenge the owner's own connection for a scripted client (curl, Python) on the app's domain? Only a datacenter address has been seen challenged. Settle it with one request before choosing a token path.
- **R2 tokens.** Does "Object Read & Write" include delete? Does CopyObject work across buckets? Settle both with a test token, which the owner creates, since agents never touch Cloudflare.
- **Cloudflare's write timeout.** Does the 30-second limit apply per write or to the whole upload? That decides whether a 90 MB upload survives on a slow link.
- **The `TDM` licence.** Does PMC's `TDM` licence code (author manuscripts) allow showing a quote? Its wording covers mining only.
- **Quoting in chat answers.** Does a passage quoted in a chat answer count as a quotation under EU Art. 5(3)(d) and Polish art. 29, or does it rest on the CC licence alone? This is for a lawyer.
- **OpenAlex's content terms.** Is OpenAlex's own data CC0, and what are the terms for its metered `content_urls`? No page read says.
- **Unpaywall.** Its field list and rate limits are unread: its data-format page rendered empty.
- **Docling's remaining model licences.** Which `docling-models` model carries which licence, and what licence do the RapidOCR weights and Tesseract language data carry?
- **Docling at this host's size.** What is its peak memory on 2 vCPU under a 1 GB cap, and does `HybridChunker` load a tokenizer offline without torch? Settle both with a timed trial.
- **OpenRouter embedding limits.** The largest array per call, tokens per call, and the rate for `qwen/qwen3-embedding-8b`, read with the OpenRouter MCP's `list-model-endpoints`. That needs `/mcp` authentication, which this session didn't have.
- **procrastinate's schema.** How its own schema and migrations fit Alembic and squawk (ADR-0024).
- **Missing evidence on chunking.** No peer-reviewed evidence was read on an abstract as a chunk versus a document vector, or on chunk sizes for scientific papers.

## Sources

| Source | Kind | Version or date read |
|---|---|---|
| [MDN, file input](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/input/file) · [webkitdirectory](https://developer.mozilla.org/en-US/docs/Web/API/HTMLInputElement/webkitdirectory) | Official docs | 2026-11-03 |
| [Cloudflare limits](https://developers.cloudflare.com/workers/platform/limits/) · [connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/) · [error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/) · [Bot Fight Mode](https://developers.cloudflare.com/bots/get-started/bot-fight-mode/) | Official docs | 2026-11-03 |
| [R2 presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/) · [CORS](https://developers.cloudflare.com/r2/buckets/cors/) · [upload objects](https://developers.cloudflare.com/r2/objects/upload-objects/) · [limits](https://developers.cloudflare.com/r2/platform/limits/) · [pricing](https://developers.cloudflare.com/r2/pricing/) · [S3 API](https://developers.cloudflare.com/r2/api/s3/api/) · [extensions](https://developers.cloudflare.com/r2/api/s3/extensions/) · [tokens](https://developers.cloudflare.com/r2/api/tokens/) · [temporary credentials](https://developers.cloudflare.com/r2/api/s3/temporary-credentials/) · [event notifications](https://developers.cloudflare.com/r2/buckets/event-notifications/) · [AWS CLI](https://developers.cloudflare.com/r2/examples/aws/aws-cli/) | Official docs | 2026-11-03 |
| [Queues pull consumers](https://developers.cloudflare.com/queues/configuration/pull-consumers/) · [Queues pricing](https://developers.cloudflare.com/queues/platform/pricing/) | Official docs | 2026-11-03 |
| [rclone S3](https://rclone.org/s3/) · [rclone copy](https://rclone.org/commands/rclone_copy/) · [aws s3 sync](https://docs.aws.amazon.com/cli/latest/reference/s3/sync.html) | Official docs | 2026-11-03 |
| [obstore sign](https://developmentseed.org/obstore/latest/api/sign/) and installed stubs | Upstream docs and code | 0.12.1 |
| [MCP versioning](https://modelcontextprotocol.io/specification/versioning) · [authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization) · [basic](https://modelcontextprotocol.io/specification/2026-07-28/basic) · [tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) · [streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) · [security best practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices) | Spec | 2026-07-28, read 2026-11-03 |
| FastMCP [authentication](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/servers/auth/authentication.mdx) · [token verification](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/servers/auth/token-verification.mdx) · [Google](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/integrations/google.mdx) · [authorization](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/servers/authorization.mdx) and installed code | Upstream docs and code | 4.1.4 |
| [Claude Code MCP](https://code.claude.com/docs/en/mcp) · [security](https://code.claude.com/docs/en/security) · [permission modes](https://code.claude.com/docs/en/permission-modes) | Official docs | 2026-11-03 |
| [OWASP API2:2023](https://api-security.owasp.org/editions/2023/en/0xa2-broken-authentication) · [ASVS 5.0.0 V7](https://github.com/OWASP/ASVS/blob/v5.0.0/5.0/en/0x16-V7-Session-Management.md) · [REST Security](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html) · [Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html) · [LLM01:2025](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) · [LLM06:2025](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/) | Standards and official guidance | 2026-11-03 |
| [PMC OA subset](https://pmc.ncbi.nlm.nih.gov/tools/openftlist/) · [OA service](https://pmc.ncbi.nlm.nih.gov/tools/oa-service/) · [new in PMC](https://pmc.ncbi.nlm.nih.gov/about/new-in-pmc/) · [PMC on AWS](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/) · [bucket README](https://pmc-oa-opendata.s3.amazonaws.com/README.txt) · [copyright](https://pmc.ncbi.nlm.nih.gov/about/copyright/) | Official docs | 2026-11-03 |
| [Europe PMC OA](https://europepmc.org/downloads/openaccess) · [REST fields](https://www.ebi.ac.uk/europepmc/webservices/rest/fields?format=json) and live searches | Official docs and API | 2026-11-03 |
| OpenAlex [rate limits](https://help.openalex.org/how-to-use-the-api/rate-limits-and-authentication) · [location object](https://help.openalex.org/api-entities/works/work-object/location-object) · [work object](https://help.openalex.org/api-entities/works/work-object) · [OA licence](https://help.openalex.org/hc/en-us/articles/41193820509079-What-is-an-OA-license) · [Unpaywall](https://help.openalex.org/access/unpaywall/) | Official docs | 2026-11-03 |
| [JATS](https://jats.nlm.nih.gov/) · [sec](https://jats.nlm.nih.gov/archiving/tag-library/1.4/element/sec.html) · [license](https://jats.nlm.nih.gov/archiving/tag-library/1.4/element/license.html) | Spec | NISO Z39.96-2024 (1.4) |
| [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/legalcode.en) · [CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode.en) · [CC0](https://creativecommons.org/publicdomain/zero/1.0/legalcode.en) · [FAQ](https://creativecommons.org/faq/) · [AI training](https://creativecommons.org/using-cc-licensed-works-for-ai-training/) | Legal code and official guidance | 2026-11-03 |
| [Directive (EU) 2019/790](https://publications.europa.eu/resource/celex/32019L0790) · [Directive 2001/29/EC](https://publications.europa.eu/resource/celex/32001L0029) · [Regulation (EU) 2024/1689](https://publications.europa.eu/resource/celex/32024R1689) | Law | 2026-11-03 |
| [Dz.U. 2024 poz. 1254](https://api.sejm.gov.pl/eli/acts/DU/2024/1254/text.html) · [Dz.U. 2022 poz. 2509](https://api.sejm.gov.pl/eli/acts/DU/2022/2509/text.html) | Law (Sejm ELI) | 2026-11-03 |
| [DOAB FAQ](https://doabooks.org/en/researchers/faq) · Podcasting 2.0 [transcript](https://podcasting2.org/docs/podcast-namespace/tags/transcript) · [license](https://podcasting2.org/docs/podcast-namespace/tags/license) | Official docs and spec | 2026-11-03 |
| [Docling PyPI](https://pypi.org/pypi/docling/json) · [docling-slim](https://pypi.org/pypi/docling-slim/json) · [docling-core](https://pypi.org/pypi/docling-core/json) · [supported formats](https://github.com/docling-project/docling/blob/v2.141.0/docs/usage/supported_formats.md) · [chunking](https://github.com/docling-project/docling/blob/v2.141.0/docs/concepts/chunking.md) · backends ([WebVTT](https://raw.githubusercontent.com/docling-project/docling/main/docling/backend/webvtt_backend.py), [EPUB](https://raw.githubusercontent.com/docling-project/docling/main/docling/backend/epub_backend.py), [JATS](https://raw.githubusercontent.com/docling-project/docling/main/docling/backend/xml/jats_backend.py)) | Upstream code and docs | 2.146.0 / v2.141.0 docs, read 2026-11-03 |
| [docling-layout-heron](https://huggingface.co/docling-project/docling-layout-heron) · [docling-models](https://huggingface.co/docling-project/docling-models/raw/main/README.md) | Model cards by the maker | 2026-11-03 |
| [Docling technical report](https://arxiv.org/html/2408.09869v5) (preprint) · [Docling, AAAI 2025 workshop](https://arxiv.org/abs/2501.17887) | Maker's engineering writing | 2026-11-03 |
| [unstructured PyPI](https://pypi.org/pypi/unstructured/json) · [chunking](https://docs.unstructured.io/open-source/core-functionality/chunking) · [pymupdf4llm LICENSE](https://raw.githubusercontent.com/pymupdf/pymupdf4llm/main/LICENSE) · [marker](https://github.com/datalab-to/marker) · [MODEL_LICENSE](https://raw.githubusercontent.com/datalab-to/marker/master/MODEL_LICENSE) | Upstream code | 2026-11-03 |
| [CSL-JSON schema](https://raw.githubusercontent.com/citation-style-language/schema/master/schemas/input/csl-data.json) · [DCMI terms](https://www.dublincore.org/specifications/dublin-core/dcmi-terms/) · [Pandoc manual](https://pandoc.org/MANUAL.html) | Specs and official docs | 2026-11-03 |
| [Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval) | Maker's engineering writing | 2024-09-19, read 2026-11-03 |
| [Qwen3-Embedding-8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B) | Model card by the maker | 2026-11-03 |
| [OpenRouter embeddings](https://openrouter.ai/docs/api/reference/embeddings) · [API reference](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request.md) · [limits](https://openrouter.ai/docs/api/reference/limits) | Official docs | 2026-11-03 |
| [PostgreSQL 18, text search features](https://www.postgresql.org/docs/18/textsearch-features.html) · [resource settings](https://www.postgresql.org/docs/18/runtime-config-resource.html) · [pgvector README](https://raw.githubusercontent.com/pgvector/pgvector/master/README.md) | Official docs | 18.6; pgvector 0.8.7 |
| [tessdoc FAQ](https://tesseract-ocr.github.io/tessdoc/FAQ.html) | Official docs | 2026-11-03 |
| procrastinate ([PyPI](https://pypi.org/project/procrastinate/), [CI](https://raw.githubusercontent.com/procrastinate-org/procrastinate/main/.github/workflows/ci.yml)) · pgqueuer ([PyPI](https://pypi.org/project/pgqueuer/)) | Upstream code | 3.10.0 · 1.6.0, read 2026-11-03 |
| Qu, Tu, Bao, [Is Semantic Chunking Worth the Computational Cost?](https://aclanthology.org/2025.findings-naacl.114/) | Peer-reviewed | Findings of NAACL 2025 |
| Chen et al., [Dense X Retrieval](https://aclanthology.org/2024.emnlp-main.845/) | Peer-reviewed | EMNLP 2024 |
| Günther et al., [Late Chunking](https://arxiv.org/abs/2409.04701) | Preprint | v3, 2025-07-07 |
