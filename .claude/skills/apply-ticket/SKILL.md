---
name: apply-ticket
description: Deliver one GitHub issue through the gated TDD delivery loop — plan gate, tests first, implement against the docs, verify, review gate, merge.
argument-hint: "[issue-number]"
disable-model-invocation: true
---

# Apply ticket #$ARGUMENTS

Implements the delivery loop of ADR-0001.

1. **Fetch.** `gh issue view $ARGUMENTS --json number,title,body,labels,state`. Stop if it is closed, or if any `Blocked by #N` issue is still open (`gh issue view N --json state`).
2. **Ground.** Read the current PRD and the requirement IDs the ticket cites, every referenced ADR, the matching `.claude/rules/`, and the area branch below. If the ticket contradicts an Accepted ADR or the Approved PRD, or needs an undocumented decision, put a doc proposal into the plan — never code around it.
3. **Plan in chat.** Effort S / M / L, the AC → test mapping, file-level steps, and any docs to touch.
4. **HUMAN GATE (plan).** `AskUserQuestion`: Approve / Split / Request changes / Cancel. No product code before Approve.
5. **L or Split** → stop and ask the user to run `/create-tickets` for the pieces. Don't implement the oversized ticket.
6. **Start.** `gh issue edit <n> --add-label status:in-progress --remove-label status:todo`, then `git checkout main && git pull && git checkout -b ticket/<n>-<slug>`.
7. **Tests first.** One failing test per AC (`/run-tdd`).
8. **Implement against the docs only.** `/run-tdd` → `/coverage` → ruff → mypy. Regenerate the OpenAPI snapshot if HTTP changed. Run `/tune-prompt` if a prompt, model ID or the router changed.
9. **Verify** every AC with evidence: test names, command output, a browser check for web tickets, the eval report for llm tickets.
10. **HUMAN GATE (review).** `AskUserQuestion`: Approve / Request changes / Reject. No commit, merge or issue close before Approve.
11. **Finish** (after Approve): commit on the ticket branch with a message referencing `#<n>`; `git checkout main && git merge --no-ff ticket/<n>-<slug>`; stay on `main`; `gh issue close <n> --comment "<summary + evidence>"` and remove the status label. If stack or workflow changed, run `/update-docs` in the same change. Push only if the user asks.

## Area branches

- **backend** — layering (ADR-0003), user scoping (ADR-0004), migrations; use `/add-endpoint` for HTTP routes.
- **web** — htmx 4 + daisyUI (ADR-0012); check the page in a browser; add a pytest-playwright spec for critical flows.
- **llm** — templates and evals (ADR-0008, ADR-0009); `TestModel` in tests; the eval report is evidence.
- **knowledge** — ADR-0011; ingestion details wait for the M3 ADR.
- **e2e** — committed specs against the running compose stack; external services mocked at the API boundary.
- **infra** — compose, Dockerfile, CI; verify with `docker compose up` and a CI run.
- **docs** — `/update-docs`; no code.

If the area label doesn't match the work, say so in the plan and ask.

## Anti-patterns

Coding before the plan gate; committing before the review gate; skipping the PRD or ADRs because the ticket "seems clear"; implementing blocked work; silently changing the locked stack.
