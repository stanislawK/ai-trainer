# Documentation

This project is document-driven: docs are written and reviewed before code, and agents implement against them.

| What | Where |
|---|---|
| Product requirements — what and why | [prd/README.md](prd/README.md) (links the current PRD) |
| Architecture decisions — how | [adr/README.md](adr/README.md) |
| Templates and how to add docs | [templates/README.md](templates/README.md) |
| Tickets | GitHub Issues in `stanislawK/ai-trainer` (labels `area:*`, `status:*`, `size:*`) |
| Agent instructions | `CLAUDE.md`, `.claude/rules/`, `.claude/skills/` at the repo root |

## Flow

PRD (what) → ADR (how) → ticket (`/create-tickets`) → delivery loop (`/apply-ticket <issue#>`) with a plan gate and a review gate.

Decisions never live only in chat. If something is undecided or contradicts a doc, change the doc first (`/update-docs`).
