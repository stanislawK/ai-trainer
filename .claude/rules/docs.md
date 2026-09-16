---
paths:
  - "docs/**/*.md"
---

# Docs rules (ADR-0001)

- The PRD owns what and why; ADRs own how. Don't put stack decisions in a PRD or product scope in an ADR.
- Create docs by copying `docs/templates/`. Never invent a new structure.
- File numbers are four digits and never change. Requirement IDs (G, B, F) are stable; a retired ID is never reused.
- Exactly one PRD is Approved. Agents write Draft, In review or Proposed; only a human sets Approved or Accepted.
- Every new doc gets its index row in the same change (`docs/prd/README.md` or `docs/adr/README.md`).
- Superseding: mark the old doc Superseded with a link to its replacement.
- An ADR that locks stack or workflow patches `CLAUDE.md`, `.claude/rules/` and `.claude/skills/` in the same change.
- References must point at real paths and real requirement IDs.
- Keep ADRs short: Context → Decision → Invariants → Consequences.
