<!--
TICKET TEMPLATE — GitHub issue body. Delete this comment before creating the issue.

Title (issue title, not in the body): "<area>: <imperative outcome>"
  - area: backend | web | llm | e2e | knowledge | infra | docs
  - one verb, ≤70 characters, no "and"
Labels: area:<area>, status:todo, size:S|M, model:sonnet|opus, risk:low|high   (L is never created — split it; model and risk per ADR-0035)

Body rules:
  - ≤200 words, not counting "Blocked by" and References (they are links, not prose)
  - "Why" is the only prose paragraph
  - 2–6 testable requirements
  - 3–7 acceptance criteria, one behaviour each, including one error/edge case
  - References point at real PRD/ADR/skill paths
  - Demo names what a human opens or runs: the account and start URL for a UI change (the
    acceptance gate, ADR-0001), or the command for one without a UI (the PR's Acceptance section, ADR-0035)
  - Touches names the modules or directories the ticket changes, plus hotspots (migration, openapi,
    templates, CLAUDE.md), so parallel tickets can be kept apart (ADR-0035)
  - Agent names model and risk with one reason: opus for risk:high, size M across 3+ modules, a new
    sport, a cross-cutting refactor or /tune-prompt work; sonnet otherwise (ADR-0035)

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

**Demo:** <account, e.g. seeded dev user / pending Google account> → `http://localhost:8000/<path>` | `<command>`

**Touches:** <modules or directories>; hotspots: <migration | openapi | templates | CLAUDE.md | none>

**Agent:** model:<sonnet|opus>, risk:<low|high> — <one-sentence reason>

**Blocked by:** #<issue> | none

**References**
- PRD: `docs/prd/NNNN-….md` (<requirement IDs>)
- ADR: `docs/adr/NNNN-….md`
- Skill: `.claude/skills/<name>/SKILL.md`
- Research: `docs/research/<topic>.md` (when one covers this area)
