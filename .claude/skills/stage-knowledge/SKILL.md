---
name: stage-knowledge
description: Stage a /research-knowledge shortlist the owner approved in chat as `proposed` knowledge sources, by PMC ID or by presigned PUT of a local file. Fills each template from the source's own metadata, reports what was staged, covered, refused or left for the owner, and never approves, deletes or re-ingests.
argument-hint: "<the approved shortlist, or which entries of the last /research-knowledge report to stage>"
---

# Stage knowledge: $ARGUMENTS (ADR-0036, ADR-0033, PRD 0011 B7)

Turns the shortlist the owner approved in chat into `proposed` sources at `/admin/knowledge`, where an admin approves them in bulk. No file's bytes pass through a model: papers are fetched by the app from PMC by ID, and files go from the owner's disk to the bucket by presigned PUT (ADR-0036 invariant 3).

## Inputs

- **The approved shortlist**: entries from `/research-knowledge`, each with its title, link, source type, tier, licence, PMC ID, DOI and PMID. Stage only entries the owner approved *in this conversation*: "all", or named ones. If the owner has approved nothing, or the entries are not in the conversation, ask which to stage and stop. A shortlist is never staged on your own initiative.
- **Environment**: `KNOWLEDGE_API_URL` and `KNOWLEDGE_API_TOKEN`, a token from `/admin/tokens`. If the client exits 3 (no token), say "No token set: create one at /admin/tokens and export KNOWLEDGE_API_TOKEN and KNOWLEDGE_API_URL" and stop. Never print the environment, and never try other credentials.
- **Local files**: a book or PDF is staged only when the owner names its path in the conversation. Never search the disk for one.

## Steps

1. **Sort the entries** into four groups:
   - **By PMC ID**: an entry with a PMC ID (a paper open on PMC). Staged with `stage-pmc`.
   - **By file**: an entry whose file the owner named a path for. Staged with `stage-file`.
   - **For the owner to obtain**: a paywalled source, a book or paper with no open licence, an entry with no PMC ID and no path, and a talk or article with no transcript file. List each with why and what to get (the file, a licence, a transcript). Never stage them, and never fetch or scrape them yourself.
   - **Not staged**: an entry whose licence is already `refused` in the report (for example `TDM`, "free to read") or that was retracted. List with the reason.
2. **Fill each template from the source's own metadata, never from memory.** Use only what the report shows from Europe PMC, the page you read or the file's own front matter. A field you did not read stays absent; write `none found` in the report, never a guess.
   - **PMC**: one manifest entry per paper, `{"pmcid", "evidence_tier", "sports", "topics", "language"}`. `evidence_tier` is `peer_reviewed`. `sports` are the app's sport keys (for example `gym`); `topics` are short lower-case words from the shortlist. The app reads the title, licence and attribution from PMC itself.
   - **File**: a template in CSL-JSON keys (`title`, `author` as `{family, given}` or `{literal}`, `issued` as `{"date-parts": [[year]]}`, `DOI`, `PMID`, `PMCID`, `ISBN`, `URL`, `abstract`, `language`) plus `evidence_tier` (`peer_reviewed`, `coaching_book` or `article_or_transcript`), `source_type` (`research_paper`, `book`, `talk_or_podcast` or `article`), `sports`, `topics` and `licence`. `licence` is `{"license_id", "license_source": "admin", "license_url", "attribution", "license_note"}` only when the report gives the licence from its source; `license_id` is `cc0`, `cc-by`, `cc-by-sa`, `cc-by-nc`, `cc-by-nc-sa`, `cc-by-nd`, `cc-by-nc-nd` or `other` (then a `license_note` saying what permission the owner holds). With no licence in the report, set `"licence": null`: the admin sets it, or it is detected from the file's front matter, before approving.
3. **Write the manifests** with the Write tool into the scratchpad, not the repo (source details stay out of git, ADR-0033): one `manifest.json` (`{"articles": [...]}`) and one template file per file. Write only metadata. A file's contents are never read, printed or put in a tool argument: pass its path.
4. **Run the client**, one command per call:
   - `uv run python scripts/knowledge_client.py stage-pmc <manifest.json>` (staged 20 at a time; prints a JSON report with one result per PMC ID).
   - `uv run python scripts/knowledge_client.py stage-file <template.json> <path-to-file>` per file. The client stages, sends the bytes from disk to the presigned link straight to the bucket, and registers the upload. Exit 1 with outcome `missing` or `expired` means the upload did not complete: report it and offer to run it again.
   - A non-zero exit prints one safe line; report it as the reason. 429 means try again later; 401 or 403 means the token is refused or expired. Do not retry with other credentials.
5. **Report in chat** in the format below.

## Report format

- **Staged**: title, and the source's status (`proposed`) with how it was staged (PMC ID or file).
- **Already covered**: title, the status of the source that covers it.
- **Refused**: title, PMC ID and the reason (`retracted`, `licence_not_admitted` with PMC's licence code, `too_large`, `unlisted_type`, or the field the template failed on). The rest of the batch was still staged.
- **Could not be read or finished**: `not_found`, `unavailable`, `missing`, `expired`, with what to do.
- **For the owner to obtain**: title, link, why it was not staged, and what to get.

End with one line: "Review and approve at /admin/knowledge."

## Boundaries

- Stages `proposed` sources only. Never approve, reject, delete or re-ingest a source: a token can't, and neither does this skill (ADR-0036 invariant 1).
- Never read a file's bytes into the conversation, and never send them through the app, a tool argument or an MCP call. The client sends them from disk by presigned PUT.
- Never print the token or a presigned link.
- Never call the app other than through `scripts/knowledge_client.py`, never connect to the production host (ADR-0023), and never create a tag or deploy.
- No athlete data in a manifest, template or report (ADR-0036 invariant 7).
- No paid model call and no eval run.
- Fill nothing from memory: a licence, a DOI or an author you did not read stays out.
