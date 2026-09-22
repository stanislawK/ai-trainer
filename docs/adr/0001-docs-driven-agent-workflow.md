# ADR 0001 — Docs-driven agent workflow

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (all), ADR-0013 |

## Context

The project is built AI-first: coding agents (Claude Code) do most of the implementation. Without a written source of truth, decisions drift between chat sessions and agents reopen settled questions. [PRD-0001](../prd/0001-ai-training-companion.md) is the product source; this ADR fixes how docs, agent instructions and tickets relate, and how one ticket is delivered.

## Decision

Use these layers:

| Layer | Owns | Lives in | Rule |
|---|---|---|---|
| PRD | What and why | `docs/prd/NNNN-*.md` | Exactly one Approved |
| ADR | How | `docs/adr/NNNN-*.md` | Context → Decision → Invariants → Consequences |
| Templates | Shape of docs and tickets | `docs/templates/` | Copy, never invent |
| `CLAUDE.md` | Always-on conventions | repo root | Points at the PRD and ADR indexes |
| Rules | Path-scoped conventions | `.claude/rules/*.md` with `paths:` frontmatter | Cite ADRs; never contradict an Accepted ADR |
| Skills | Repeatable workflows | `.claude/skills/<name>/SKILL.md` | Checklists with human gates |
| Hooks | Hard enforcement | `.claude/settings.json` | Added from M0, once code exists |
| Tickets | One shippable slice | GitHub Issues, `stanislawK/ai-trainer` | Body from `docs/templates/ticket.md` |

**Tracker.** GitHub Issues through the `gh` CLI. Labels: `area:{backend,web,llm,e2e,knowledge,infra,docs}`, `status:{todo,in-progress,in-review}`, `size:{S,M}`. Done means closed. The first `/create-tickets` run creates missing labels.

**Branches and pull requests.** Every change lives on its own branch from `main` and reaches `main` only through a pull request that a human merges. Nothing is committed or pushed to `main` directly.

- Ticket branch: `<area>/<issue#>-<slug>`, where `<area>` is the ticket's `area:` label and `<slug>` is the issue title without its `area:` prefix, lowercase kebab-case, at most five words — e.g. `infra/3-start-stack-with-docker-compose`.
- Doc-only work without an issue: `docs/<slug>`.
- The PR body carries `Closes #<issue#>`, so merging it closes the issue.

**ADR research.** Before a new ADR's Decision or Alternatives considered is filled in, factual claims about a third-party library or service are checked against current docs (context7) or `WebSearch`, not written from memory. Unresolved items are flagged explicitly in Consequences instead of being carried as an untested assumption.

**Delivery loop** (`/apply-ticket <issue#>`):

1. Fetch the ticket (status, AC, blockers).
2. Ground it in the docs: the current PRD, the referenced ADRs and the matching rules and skills.
3. Estimate S / M / L and write a step plan in chat.
4. **Plan gate** — Approve / Split / Request changes / Cancel.
5. L → stop and split with `/create-tickets`.
6. Label `status:in-progress`, create the ticket branch from an up-to-date `main`.
7. Tests first: every AC maps to a failing test.
8. Implement against the docs: TDD → coverage → lint → types → contract snapshot.
9. Verify every AC with evidence.
10. **Review gate** — Approve / Request changes / Reject.
11. Commit on the ticket branch, push it, open a PR against `main` with the AC evidence and `Closes #<issue#>`, and move the issue to `status:in-review`. A human merges the PR, which closes the issue. Update docs in the same PR if stack or workflow changed.

Human gates use Claude Code's `AskUserQuestion` with exactly those options.

### Invariants

1. Never bury a product or stack decision only in chat: update the PRD or add an ADR first.
2. Exactly one PRD is Approved. Only a human sets a PRD to Approved or an ADR to Accepted.
3. An ADR that locks stack or workflow patches `CLAUDE.md`, rules and skills in the same change.
4. No product code before the plan gate; no commit, push or PR before the review gate.
5. `/create-tickets` never creates an issue before its draft gate is approved.
6. If a ticket contradicts an Accepted ADR or the Approved PRD, stop and propose a doc change in the plan gate. Never code around it.
7. Never implement a ticket whose `Blocked by` issue is still open. Never implement an L ticket.
8. A new ADR's claims about a third-party library or service are checked against current docs or web search before the ADR is marked Proposed.
9. `main` changes only when a human merges a pull request. Agents never commit to, push to or merge into `main`, and never merge their own PR.

## Alternatives considered

- Linear (previous project) — needs an MCP server and a second tool; GitHub Issues sits next to the code and `gh` already works.
- Markdown tickets in the repo — no board, notifications or cross-linking with commits.

## Consequences

- Implemented by `CLAUDE.md`, `.claude/rules/docs.md` and the skills `create-tickets`, `apply-ticket` and `update-docs`.
- `CLAUDE.md` and rules are guidance, not enforcement. M0 adds hooks where a hard stop is worth it (e.g. no commits on `main`, formatting after edits).
- Amended 2026-09-22 at the project owner's direction: delivery moved from local merges into `main` to pull requests merged by a human; ticket branches renamed from `ticket/<n>-<slug>` to `<area>/<n>-<slug>`; `status:in-review` added.
- Moving to Linear later means a new ADR superseding the tracker section, plus skill changes.
