---
name: research-knowledge
description: Find well-sourced candidate documents for the knowledge base on a topic, ranked by evidence tier, rejecting unvetted content and leaving out what the owner already covers. Reports in chat; never uploads, commits or spends on paid model calls.
argument-hint: "<topic> [covered: <sources the owner already uploaded>]"
---

# Research knowledge: $ARGUMENTS (ADR-0033, ADR-0036, PRD 0011 B7)

Finds sources worth adding to `/admin/knowledge`. Agents never upload directly: once `/stage-knowledge` exists, the shortlist the owner approves is staged as `proposed` sources that an admin approves in bulk; until then the owner uploads (ADR-0036). This skill itself stages nothing.

## Inputs

- **Topic**: the part of `$ARGUMENTS` before the literal marker `covered:` (for example `hypertrophy volume`), or all of it when there is no marker. If it is empty, ask for one and stop.
- **Covered list**: what production already holds, from two places that are merged. Agents can't reach production's `knowledge_sources` (ADR-0023), so never look for it in a database.
  - **Production's list**: run `uv run python scripts/knowledge_client.py list`. It reads `KNOWLEDGE_API_URL` and `KNOWLEDGE_API_TOKEN` from the environment, calls the token route, and prints a JSON array with each source's title, status, PMC ID, DOI, PMID, ISBN and URL.
    - Exit 0: use the array. Every status counts as covered, including `proposed`, `rejected` and `failed`, because the owner already knows that source; the status goes in the "already covered" table so the owner can re-add a failed one by choice.
    - It exits 3 (no token set): write "No token set: skipping the production covered list." in the report, and carry on. This is not a failure.
    - It exits 1, or any other code: report its one-line message (or the code) as the reason the check was skipped, and carry on. A 429 means try again later; a 401 or 403 means the token is refused or expired. Never print the environment or retry with other credentials.
  - **Pasted or filed**: what the owner pasted in the arguments, or a file whose path the owner names. Use it as is, beside the production list.
  - **Neither given**: start the report with "No covered list given: treating coverage as empty." and carry on.

## Steps

1. **Search.** Use web search in standard mode, several queries per topic: reviews and meta-analyses, coaching books, talks and podcasts by credentialed people, long-form articles. Read a page before listing it; never list a source from a search snippet or from memory.
   - **Peer-reviewed papers: Europe PMC.** Query `https://www.ebi.ac.uk/europepmc/webservices/rest/search` with `resultType=core`, `format=json` and `OPEN_ACCESS:y` in `query`, using only the topic words. Take `pmcid`, `doi`, `pmid`, `license` and `isOpenAccess` from each result; write `none found` for a field it lacks. Send at most 10 requests per second (ADR-0036): one at a time, a few per topic.
   - **Licence**: Europe PMC's `license` is lower case text such as `cc by`. Map it to the closed set (`cc by` to `cc-by`, `cc by-nc` to `cc-by-nc`, `cc0` to `cc0`, and so on); text that fits none is `other` followed by the raw text. No `license` means "unknown: owner to check", never a guess.
2. **Screen.** Reject a candidate with no credentials or no evidence, and record the reason. Examples: an influencer video with no named qualification; a claim with no cited study or data; an unnamed author; a product page; a source whose author's credentials you couldn't confirm. Popularity is never a credential.
3. **Drop the covered.** Match each candidate against the covered list by PMC ID (compare as `PMC` plus digits, so `pmc123` and a bare `123` both match `PMC123`), else DOI (compare lower case, without a `https://doi.org/` prefix), else URL, else by normalised title (lower case, punctuation and spacing removed) and author. A match is left out of the candidates and named under "Left out: already covered" with the identifier it matched on.
4. **Rank by evidence tier**, highest first:
   1. peer-reviewed (systematic reviews and meta-analyses before single trials);
   2. coaching books;
   3. expert talks and podcasts;
   4. articles.

   A source that fits none of these (a preprint, a position stand, a guideline) gets the nearest tier with the doubt written in its "Why credible" line; never invent a fifth tier. The owner assigns the final tier at upload, and the tier list itself is still open (`docs/research/knowledge-base.md`, open questions).

   The tier names the kind of source, not the certainty of any claim in it (`docs/research/knowledge-base.md`, finding 5). Within a tier, put the best-credentialed and most recent first.
5. **Report in chat** in the format below.

## Report format

Start with the covered-list notice when it applies, then:

**Candidates**, one entry each:
- **Title, author and year**
- **Link**
- **Source type**: peer-reviewed paper, book, talk, podcast or article
- **Proposed tier**: 1 to 4 from the ranking above
- **License note**: what you can say about reuse (open access with its license, a book bought and uploaded for private use, a transcript for a talk). When unclear, write "unknown: owner to check". ADR-0033 invariant 5 needs the owner to settle it before ingestion. Name the licence by ADR-0036's closed set where you can (`cc0`, `cc-by`, `cc-by-sa`, `cc-by-nc`, `cc-by-nc-sa`, `cc-by-nd`, `cc-by-nc-nd`, `other`), and give the licence's closed-set value with its source (`Europe PMC` or the page read).
- **Identifiers**: PMC ID and DOI, each `none found` when absent, since papers are staged by PMC ID. Add the PMID when Europe PMC returned one.
- **Why credible**: one line on the author's credentials or the evidence.

**Rejected**: a table of title or link, and the reason.

**Left out: already covered**: a table of title, the identifier matched (PMC ID, DOI, URL or title), its status when production's list gave one, and which list it came from (production or pasted). **Cut for length**: a count, when it applies.

## Boundaries

- Reports in chat only. Write no file and make no commit: source details stay out of git (ADR-0033).
- Never upload, and never call the app or `ObjectStoragePort`. The one app call is `scripts/knowledge_client.py list`, which only reads the token route.
- Europe PMC is the only literature API the skill calls, and only for search (ADR-0036).
- No paid model call: no OpenRouter `send-message`, `send-batch` or other paid tool, and no eval run.
- No athlete data in a search query or the report.
- Keep the candidates reviewable: about ten at most. Credible sources cut only for length are not rejections; report them as a count under "Cut for length", and keep the Rejected table for sources that failed the screen.
