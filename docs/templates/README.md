# Templates

Copy a template to create a new doc or ticket. Never invent a new structure.

| Template | Used for |
|---|---|
| [prd.md](prd.md) | A new PRD or PRD revision in `docs/prd/` |
| [adr.md](adr.md) | A new ADR in `docs/adr/` |
| [ticket.md](ticket.md) | The body of a GitHub issue (created with `/create-tickets`) |

## Numbering

- File names are `NNNN-kebab-title.md`: four digits, the next free number in that folder. Never renumber a file.
- PRD requirement IDs (`G1`, `B1`, `F1`, …) stay stable across PRD revisions, so ADRs and tickets can keep citing them. A removed requirement's ID is retired and never reused. New requirements get the next free number in their group.

## Status vocabulary

| Doc | Statuses |
|---|---|
| PRD | Draft · In review · Approved · Deprecated · Superseded (link the replacement) |
| ADR | Proposed · Accepted · Deprecated · Superseded (link the replacement) |
| Ticket | label `status:todo` · label `status:in-progress` · label `status:in-review` (PR open) · Done = closed issue (its PR merged) |

Only one PRD is Approved at a time. Only a human sets a PRD to Approved or an ADR to Accepted; agents write Draft, In review or Proposed.

## Add a PRD revision

Run `/update-docs prd <title>`, or by hand:

1. Copy `prd.md` to `docs/prd/NNNN-kebab-title.md` using the next number.
2. Fill every section. Delete a section only if it truly does not apply. Keep existing requirement IDs.
3. Set status to Draft, then In review when it is ready to read.
4. After human review, a human sets it to Approved and marks the previous Approved PRD as Superseded (with a link) or Deprecated.
5. Update the table and the "Current" line in `docs/prd/README.md`.

## Add an ADR

Run `/update-docs adr <title>`, or by hand:

1. Copy `adr.md` to `docs/adr/NNNN-kebab-title.md` using the next number.
2. Set status to Proposed and today's date.
3. Add a row to `docs/adr/README.md`.
4. After human review, a human sets it to Accepted. If it replaces an ADR, mark the old one "Superseded by ADR-NNNN".
5. If it locks stack or workflow, patch `CLAUDE.md`, `.claude/rules/` and `.claude/skills/` in the same change.

## Add a ticket

Run `/create-tickets <scope>`. Tickets live in GitHub Issues (`stanislawK/ai-trainer`). The body is copied from `ticket.md`; the skill shows every draft for approval before creating anything.
