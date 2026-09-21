<!--
TICKET TEMPLATE — GitHub issue body. Delete this comment before creating the issue.

Title (issue title, not in the body): "<area>: <imperative outcome>"
  - area: backend | web | llm | e2e | knowledge | infra | docs
  - one verb, ≤70 characters, no "and"
Labels: area:<area>, status:todo, size:S|M   (L is never created — split it)

Body rules:
  - ≤200 words
  - "Why" is the only prose paragraph
  - 2–6 testable requirements
  - 3–7 acceptance criteria, one behaviour each, including one error/edge case
  - References point at real PRD/ADR/skill paths

Slice by user-visible or independently shippable contract, not by layer ("add repository" is a bad ticket).
Size ~0.5–1 day. Split when the title needs "and", there are >7 AC, it mixes refactor and feature,
or it cannot be verified until later work lands.
-->

**Why:** <one or two lines>

**Requirements**
- <testable requirement>
- <testable requirement>

**Acceptance criteria**
- [ ] <behaviour>
- [ ] <behaviour>
- [ ] <error or edge case>

**Out of scope** (optional)
- <item>

**Notes** (optional)
- <item>

**Blocked by:** #<issue> | none

**References**
- PRD: `docs/prd/NNNN-….md` (<requirement IDs>)
- ADR: `docs/adr/NNNN-….md`
- Skill: `.claude/skills/<name>/SKILL.md`
