---
name: research-knowledge
description: Find well-sourced candidate documents for the knowledge base on a topic, ranked by evidence tier, rejecting unvetted content and leaving out what the owner already covers. Reports in chat; never uploads, commits or spends on paid model calls.
argument-hint: "<topic> [covered: <sources the owner already uploaded>]"
---

# Research knowledge: $ARGUMENTS (ADR-0033, ADR-0036, PRD 0011 B7)

Finds sources worth adding to `/admin/knowledge`. Agents never upload directly: once `/stage-knowledge` exists, the shortlist the owner approves is staged as `proposed` sources that an admin approves in bulk; until then the owner uploads (ADR-0036). This skill itself stages nothing.

## Inputs

- **Topic**: the part of `$ARGUMENTS` before the literal marker `covered:` (for example `hypertrophy volume`), or all of it when there is no marker. If it is empty, ask for one and stop.
- **Covered list**: what the owner already uploaded, pasted in the arguments or in a file whose path the owner names. Agents can't reach production's `knowledge_sources` (ADR-0023), so never look for it in a database.
  - **Given**: use it as is.
  - **Missing or empty**: start the report with "No covered list given: treating coverage as empty." and carry on.

## Steps

1. **Search.** Use web search in standard mode, several queries per topic: reviews and meta-analyses, coaching books, talks and podcasts by credentialed people, long-form articles. Read a page before listing it; never list a source from a search snippet or from memory.
2. **Screen.** Reject a candidate with no credentials or no evidence, and record the reason. Examples: an influencer video with no named qualification; a claim with no cited study or data; an unnamed author; a product page; a source whose author's credentials you couldn't confirm. Popularity is never a credential.
3. **Drop the covered.** Match each candidate against the covered list by DOI or URL, else by normalised title and author. A match is left out of the candidates; only its count is reported.
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
- **License note**: what you can say about reuse (open access with its license, a book bought and uploaded for private use, a transcript for a talk). When unclear, write "unknown: owner to check". ADR-0033 invariant 5 needs the owner to settle it before ingestion. Name the licence by ADR-0036's closed set where you can (`cc0`, `cc-by`, `cc-by-sa`, `cc-by-nc`, `cc-by-nc-sa`, `cc-by-nd`, `cc-by-nc-nd`, `other`), and give the PMC ID and DOI when the source has them, since papers are staged by PMC ID.
- **Why credible**: one line on the author's credentials or the evidence.

**Rejected**: a table of title or link, and the reason.

**Left out: already covered**: a count. **Cut for length**: a count, when it applies.

## Boundaries

- Reports in chat only. Write no file and make no commit: source details stay out of git (ADR-0033).
- Never upload, and never call the app or `ObjectStoragePort`.
- No paid model call: no OpenRouter `send-message`, `send-batch` or other paid tool, and no eval run.
- No athlete data in a search query or the report.
- Keep the candidates reviewable: about ten at most. Credible sources cut only for length are not rejections; report them as a count under "Cut for length", and keep the Rejected table for sources that failed the screen.
