---
name: apply-ticket
description: Deliver one GitHub issue through the gated TDD delivery loop — plan gate, tests first, implement against the docs, verify, acceptance gate, review gate, pull request.
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
6. **Start.** `gh issue edit <n> --add-label status:in-progress --remove-label status:todo`, then `git checkout main && git pull && git checkout -b <area>/<n>-<slug>` — `<area>` from the ticket's `area:` label, `<slug>` the issue title without its `area:` prefix, lowercase kebab-case, at most five words (e.g. `infra/3-start-stack-with-docker-compose`).
7. **Tests first.** One failing test per AC (`/run-tdd`).
8. **Implement against the docs only.** `/run-tdd` → `/coverage` → ruff → mypy. Regenerate the OpenAPI snapshot if HTTP changed. Run `/tune-prompt` if a prompt, model ID or the router changed.
9. **Verify** every AC with evidence: test names, command output, a browser check for web tickets, the eval report for llm tickets. A visual/design check (`/implement-design`'s parity screenshots, or any other Playwright screenshot comparison) is never self-certified — its screenshots are saved under `.acceptance/<n>/` and a human confirms the match at the acceptance gate before this AC counts as verified (ADR-0019). Then, for each AC, spawn a fresh-context agent whose only job is to try to disprove the "this AC is done" claim, given just the diff and the AC text (not the implementer's reasoning or plan). An AC only goes to the human gates as verified if it survives; anything a skeptic successfully challenges gets fixed first, and the challenge is shown alongside the fix in the gate summary.
10. **HUMAN GATE (acceptance).** A human tries the change before anyone reads the diff (ADR-0001). Never self-certify it.
    - **Stack.** The human keeps `make start` running in their own terminal. It serves the checked-out ticket branch and reloads code, templates and CSS live. Confirm `make health` first. If it's down, or the ticket adds a migration or a dependency (both are picked up only at start), ask the human to restart `make start`, and wait. Never run `make up` alongside it: they use the same ports.
    - **Script.** In the gate message, write a numbered step-by-step click-through, one section per AC or behavior. Each section gives:
      - the **account**: which one and how to get it into the needed state (an admin email from `ADMIN_EMAILS`, a second Google account left `pending`, or the seeded `scripts/dev_session.py` user);
      - the **start URL**, as a full `http://localhost:8000/…` link;
      - **each action** spelled out ("click *Continue with Google*", "type `…` in the composer and press Enter");
      - the **expected result** after each step, with the viewport (390×844 phone or desktop) and theme where layout matters.
      At least one section exercises an edge or error case from the AC. A ticket with no UI (llm, infra, backend-only) gets the exact commands to run or watch, with their expected output, instead of clicks.
    - **Screenshots.** When the ticket saved any (the `/implement-design` parity check or another comparison):
      - list every file as a clickable link under `.acceptance/<n>/`, paired mock | app per viewport and theme;
      - give `open .acceptance/<n>` to browse the folder;
      - include the probe table.
    - **Ask.** Make one `AskUserQuestion` call with:
      - the click-through question: Pass / Fail / Blocked;
      - when screenshots exist, a second question, "Screenshots match the mock": Approve / Request changes.
    - **Loop.** On Fail, Blocked or Request changes, ask what went wrong if the answer doesn't say. Then fix it (step 8), re-verify (step 9) and run this gate again with an updated script. Record every round.
    - Keep `.acceptance/<n>/` after the gate, and never commit it.
11. **HUMAN GATE (review).** `AskUserQuestion`: Approve / Request changes / Reject. No commit, push or PR before Approve.
12. **Finish** (after Approve), all on the ticket branch:
    - If stack or workflow changed, run `/update-docs` so the doc change ships in the same PR.
    - If the ticket changed how to build, run or operate the app (a new service, a new local-dev step, a new command), update `README.md` and the `Makefile` in the same change (`.claude/rules/operability.md`).
    - Commit with a message referencing `#<n>`.
    - `git push -u origin <area>/<n>-<slug>`.
    - `gh pr create --base main --title "<issue title> (#<n>)" --body-file <file>`. The body holds the summary, the AC → evidence table, the skeptic challenges and their fixes, an **Acceptance** section (the script, the answer, and any failed rounds with their fixes), and `Closes #<n>`.
    - `gh issue edit <n> --add-label status:in-review --remove-label status:in-progress`.
    - Report the PR URL and stop. A human reviews and merges it on GitHub; the merge closes the issue. Never commit to, push to or merge into `main`, and never merge the PR yourself.

## Area branches

- **backend** — layering (ADR-0003), user scoping (ADR-0004), migrations; use `/add-endpoint` for HTTP routes.
- **web** — htmx 4 + daisyUI (ADR-0012, ADR-0019); build designed screens from their Claude Design mock with `/implement-design`, whose Playwright parity screenshots (390×844 and 1440×900, dark and light) are saved under `.acceptance/<n>/parity/` and confirmed by a human at the acceptance gate, not by the agent's own read of them. Verify other behavior ad hoc with the Playwright MCP tools against the running app; add a pytest-playwright spec under `tests/e2e/` only for a flow critical enough to guard against regression forever, not by default (ADR-0013).
- **llm** — templates and evals (ADR-0008, ADR-0009); `TestModel` in tests; the eval report is evidence.
- **knowledge** — ADR-0011; ingestion details wait for the M3 ADR.
- **e2e** — committed specs against the running compose stack; external services mocked at the API boundary; signed-in pages through the seeded session from `scripts/dev_session.py`; behavior, not pixels.
- **infra** — compose, Dockerfile, CI; verify with `make start` / `docker compose up` and a CI run; the acceptance script is the commands the human runs.
- **docs** — `/update-docs`; no code.

If the area label doesn't match the work, say so in the plan and ask.

## Anti-patterns

Coding before the plan gate; self-certifying the acceptance gate, or reaching the review gate before it passes; an acceptance script that says "check the page works" instead of naming each click and its expected result; committing or pushing before the review gate; committing to, pushing to or merging into `main`; merging your own PR; skipping the PRD or ADRs because the ticket "seems clear"; implementing blocked work; silently changing the locked stack.
