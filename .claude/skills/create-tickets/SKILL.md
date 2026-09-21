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
   - In a foundations milestone there is no user-visible feature to slice by. The contract is then a command or a gate — "`docker compose up` brings the stack up", "CI fails on OpenAPI drift" — which still isn't slicing by layer. "Add a repository" still is.
   - The title's `<area>` must come from the label set, so a ticket spanning layers takes the area of its substance, not its surface: Google sign-in is `backend`, not `web`.
   - An ADR's milestone note covers its *feature*; its invariants bind as soon as any ticket touches them. ADR-0014 is scheduled M1, but its no-wall-clock rule binds the first ticket that stores a timestamp — so the `Clock` port comes forward with it.
5. **Draft** every ticket from `docs/templates/ticket.md`: title `<area>: <imperative outcome>` (≤70 characters, one verb, no "and"), body ≤200 words, 2–6 requirements, 3–7 AC including one error or edge case, `Blocked by`, and References to real PRD/ADR/skill paths with requirement IDs. Order the tickets by dependency. Auth- and gateway-shaped tickets run past 200 words easily — spend the budget on acceptance criteria, not on prose.
6. **HUMAN GATE.** Show all drafts in chat, then `AskUserQuestion`: Approve / Request changes / Cancel. On Request changes, revise and ask again. On Cancel, stop.
7. **Create** (only after Approve):
   - Check labels with `gh label list`; create any missing ones from `area:{backend,web,llm,e2e,knowledge,infra,docs}`, `status:{todo,in-progress}`, `size:{S,M}` with `gh label create`.
   - Write each body to a scratchpad file with a placeholder token per blocker, then create in dependency order with `gh issue create --title "<title>" --body-file <file> --label "area:<area>,status:todo,size:<size>"`. Every blocker already has its number by then, so substitute at creation and skip the patch pass entirely.
   - Use `gh issue edit` only for a ticket that had to be created out of order, or to fix a body after creation.
   - Check before reporting: no placeholder survives, every blocker number is lower than its own (an acyclic graph), each body has 3–7 AC and stays within 200 words, and every cited path exists.
8. **Report** a table: issue number, title, labels, blocked by.

Never create an L ticket, never create issues before Approve, and never reference a path that doesn't exist.
