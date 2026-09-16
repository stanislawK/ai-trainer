---
name: create-tickets
description: Product-manager workflow that slices PRD scope into GitHub Issues from the ticket template, with a human approval gate before anything is created.
argument-hint: "[milestone or requirement IDs, e.g. M0 or B1-B3]"
disable-model-invocation: true
---

# Create tickets for: $ARGUMENTS

Role: product manager. Implements ADR-0001. Never write to GitHub before the draft gate is approved.

1. **Ground.** Read `docs/prd/README.md` and the current PRD (delivery mapping, requirement IDs), `docs/adr/README.md` and the relevant ADRs, and `docs/templates/ticket.md`. If the PRD is not Approved, say so and ask whether to continue with drafts.
2. **Clarify.** Ask about anything ambiguous in the scope with `AskUserQuestion`. If a product or stack decision is missing, propose `/update-docs` instead of guessing.
3. **Check duplicates.** For each candidate, run `gh issue list --state all --search "<keywords>"`. Skip or reuse duplicates.
4. **Slice.** One independently shippable, user-visible contract per ticket, sized S or M (~0.5–1 day). Split when the title needs "and", there are more than 7 AC, it mixes refactor and feature, or it can't be verified until later work lands. Never slice by layer.
5. **Draft** every ticket from `docs/templates/ticket.md`: title `<area>: <imperative outcome>` (≤70 characters, one verb, no "and"), body ≤200 words, 2–6 requirements, 3–7 AC including one error or edge case, `Blocked by`, and References to real PRD/ADR/skill paths with requirement IDs. Order the tickets by dependency.
6. **HUMAN GATE.** Show all drafts in chat, then `AskUserQuestion`: Approve / Request changes / Cancel. On Request changes, revise and ask again. On Cancel, stop.
7. **Create** (only after Approve):
   - Check labels with `gh label list`; create any missing ones from `area:{backend,web,llm,e2e,knowledge,infra,docs}`, `status:{todo,in-progress}`, `size:{S,M}` with `gh label create`.
   - Create blockers first. For each ticket, write the body to a scratchpad file and run `gh issue create --title "<title>" --body-file <file> --label "area:<area>,status:todo,size:<size>"`.
   - Replace `Blocked by` placeholders with real issue numbers using `gh issue edit`.
8. **Report** a table: issue number, title, labels, blocked by.

Never create an L ticket, never create issues before Approve, and never reference a path that doesn't exist.
