---
name: update-docs
description: Add a PRD revision or a new ADR from the templates, update the indexes, supersede old docs, and patch CLAUDE.md, rules and skills when stack or workflow changes.
argument-hint: "[prd|adr] [short title]"
---

# Update docs: $ARGUMENTS (ADR-0001)

**Amending an existing ADR.** When an ADR's Consequences hand a fact to a later ticket (a verified version minimum, a chosen tool), record it in that ADR in place, cite the source and the ticket, and keep its status; skip steps 2 and 4–7. Changing a Decision or an Invariant needs a new ADR that supersedes it, or an amendment the project owner explicitly directs, noted with its date in Consequences.

1. **Pick the doc type.** What or why → a PRD revision. How → an ADR. If unsure, ask.
2. **Number.** Take the next free four-digit number in `docs/prd/` or `docs/adr/`.
3. **Research (ADR only).** Before drafting Decision or Alternatives considered, verify factual claims about a third-party library or service — use context7 for library/API behavior and `WebSearch` for comparing alternatives, current best practices and known pitfalls. Cite sources as links inline. Surface anything you couldn't resolve as a flagged item in Consequences, not a silent guess (ADR-0001).
4. **Copy the template** from `docs/templates/` and fill every section. PRD revisions keep existing requirement IDs; new requirements get the next free ID.
5. **Status.** PRD: Draft, or In review when ready. ADR: Proposed. Never set Approved or Accepted — a human does that.
6. **Index.** Add the row to `docs/prd/README.md` or `docs/adr/README.md` in the same change.
7. **Supersede.** When the new doc replaces another, note it in the new doc's Related field. After the human approves or accepts, mark the old doc Superseded with a link to the new one.
8. **Propagate.** If the ADR locks stack or workflow, patch `CLAUDE.md`, the affected `.claude/rules/` and `.claude/skills/` in the same change.
9. **Check.** Every path and requirement ID you cite exists.
10. **Ship.** Outside a ticket, work on a `docs/<slug>` branch from an up-to-date `main`, then commit, push and open a PR against `main` with `gh pr create` once the user approves. Inside `/apply-ticket`, the doc change rides in the ticket's PR. Never commit to, push to or merge into `main` (ADR-0001).
