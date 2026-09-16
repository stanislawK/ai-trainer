---
name: update-docs
description: Add a PRD revision or a new ADR from the templates, update the indexes, supersede old docs, and patch CLAUDE.md, rules and skills when stack or workflow changes.
argument-hint: "[prd|adr] [short title]"
---

# Update docs: $ARGUMENTS (ADR-0001)

1. **Pick the doc type.** What or why → a PRD revision. How → an ADR. If unsure, ask.
2. **Number.** Take the next free four-digit number in `docs/prd/` or `docs/adr/`.
3. **Copy the template** from `docs/templates/` and fill every section. PRD revisions keep existing requirement IDs; new requirements get the next free ID.
4. **Status.** PRD: Draft, or In review when ready. ADR: Proposed. Never set Approved or Accepted — a human does that.
5. **Index.** Add the row to `docs/prd/README.md` or `docs/adr/README.md` in the same change.
6. **Supersede.** When the new doc replaces another, note it in the new doc's Related field. After the human approves or accepts, mark the old doc Superseded with a link to the new one.
7. **Propagate.** If the ADR locks stack or workflow, patch `CLAUDE.md`, the affected `.claude/rules/` and `.claude/skills/` in the same change.
8. **Check.** Every path and requirement ID you cite exists.
