---
name: create-tickets
description: Product-manager workflow that slices PRD scope into GitHub Issues from the ticket template, with a human approval gate before anything is created.
argument-hint: "[milestone or requirement IDs, e.g. M0 or B1-B3]"
disable-model-invocation: true
---

# Create tickets for: $ARGUMENTS

Role: product manager. Implements ADR-0001. Never write to GitHub before the draft gate is approved.

1. **Ground.** Read `docs/prd/README.md` and the current PRD (delivery mapping, requirement IDs), `docs/adr/README.md` and the relevant ADRs, and `docs/templates/ticket.md`. If the PRD is not Approved, say so and ask whether to continue with drafts.
2. **Clarify.** Ask about anything ambiguous in the scope with `AskUserQuestion`. If a product or stack decision is missing, propose `/update-docs` instead of guessing. Expect the gap to surface while drafting a testable AC, not while reading the ADR — an invariant that says *what* to record but not *where* reads as complete until you try to verify it.
3. **Check duplicates.** For each candidate, run `gh issue list --state all --search "<keywords>"`. Skip or reuse duplicates.
4. **Slice.** One independently shippable, user-visible contract per ticket, sized S or M (~0.5–1 day). Split when the title needs "and", there are more than 7 AC, it mixes refactor and feature, or it can't be verified until later work lands. Never slice by layer.
   - A migration that drops, renames or narrows something that holds data is a **contract** step. It gets its own ticket, titled `… (contract)`, blocked by the ticket whose release stopped using that data (ADR-0024).
   - In a foundations milestone there is no user-visible feature to slice by. The contract is then a command or a gate — "`docker compose up` brings the stack up", "CI fails on OpenAPI drift" — which still isn't slicing by layer. "Add a repository" still is.
   - The title's `<area>` must come from the label set, so a ticket spanning layers takes the area of its substance, not its surface: Google sign-in is `backend`, not `web`.
   - A `web` ticket that implements a screen names its Claude Design file and target template from `docs/design/README.md` in References. If the screen has no design yet, the design comes first; don't ticket the UI blind (ADR-0019). A design ticket asks for Liquid Glass v4 for every new element (`mat-content` for content, lists and sheets included, `lg-*` controls on it, no glass on glass; ADR-0019, 2026-10-24) and for the mock to load `glass-v2.css`.
   - Every ticket is demoable (ADR-0001 invariant 11): a human can see it work at the acceptance gate, in the running app (`make start`) or through a command. If a slice can't be shown on its own, merge it into the ticket that makes it visible, or name the command that demonstrates it.
   - **Parallel lanes (ADR-0035).** Tickets run concurrently, one worktree each, so slice to keep them apart:
     - prefer slices that touch disjoint modules;
     - chain tickets that add a migration (each `Blocked by` the previous), so at most one is in flight;
     - never leave two tickets that each take a new ADR or PRD number unchained.
   - **Research.** An area with a document in `docs/research/` gets it in each implementing ticket's References. An area that needs research first gets a `docs:` research ticket that writes the document from `docs/templates/research.md`, and the implementing tickets are `Blocked by` it (ADR-0001, 2026-10-31).
   - An ADR's milestone note covers its *feature*; its invariants bind as soon as any ticket touches them. ADR-0014 is scheduled M1, but its no-wall-clock rule binds the first ticket that stores a timestamp — so the `Clock` port comes forward with it.
5. **Draft** every ticket from `docs/templates/ticket.md`: title `<area>: <imperative outcome>` (≤70 characters, one verb, no "and"), body ≤200 words not counting `Blocked by` and References, 2–6 requirements, 3–7 AC including one error or edge case, a **Demo** line (the account and start URL, or the command a human runs), a **Touches** line (the modules or directories it changes, plus any hotspot: `migration`, `openapi`, `templates`, `CLAUDE.md`), an **Agent** line (`model:` and `risk:` with one sentence of why, from ADR-0035's routing rules), `Blocked by`, and References to real PRD/ADR/skill paths with requirement IDs. Order the tickets by dependency. `Blocked by` and References are links, not prose, so citing another ADR never costs an acceptance criterion. Auth- and gateway-shaped tickets still run past 200 words easily — spend the budget on acceptance criteria, not on prose.
6. **HUMAN GATE.** Show all drafts in chat, followed by a **wave table**: wave 1 lists the tickets that can start now, in parallel (ready, and no shared `Touches` entry or hotspot between them); wave 2 lists those unblocked by wave 1; and so on. Then `AskUserQuestion`: Approve / Request changes / Cancel. On Request changes, revise and ask again. On Cancel, stop.
7. **Create** (only after Approve):
   - Check labels with `gh label list`; create any missing ones from `area:{backend,web,llm,e2e,knowledge,infra,docs}`, `status:{todo,in-progress,in-review}`, `size:{S,M}`, `model:{sonnet,opus}`, `risk:{low,high}` with `gh label create`.
   - Write each body to a scratchpad file with a placeholder token per blocker, then create in dependency order with `gh issue create --title "<title>" --body-file <file> --label "area:<area>,status:todo,size:<size>,model:<model>,risk:<risk>"`. Every blocker already has its number by then, so substitute at creation and skip the patch pass entirely.
   - Use `gh issue edit` only for a ticket that had to be created out of order, or to fix a body after creation.
   - Check before reporting: no placeholder survives, every blocker number is lower than its own (an acyclic graph), each body has 3–7 AC, a Demo, a Touches and an Agent line, and stays within 200 words before `Blocked by`, and every cited path exists.
8. **Report** a table: issue number, title, labels, blocked by, wave.

Never create an L ticket, never create issues before Approve, and never reference a path that doesn't exist.
