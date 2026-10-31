---
name: apply-ticket
description: Deliver one GitHub issue in its own worktree through the TDD delivery loop — plan checked by a critic (human plan gate when risky), tests first, implement against the docs, verify, acceptance gate for UI tickets, automated review, draft pull request. `--review` handles the PR's review comments.
argument-hint: "[issue-number] [--review]"
disable-model-invocation: true
---

# Apply ticket $ARGUMENTS

Implements the delivery loop of ADR-0001 as amended by ADR-0035. With `--review` in the arguments, skip to **Review mode** below.

1. **Fetch.** `gh issue view <n> --json number,title,body,labels,state`. Stop if it is closed, if any `Blocked by #N` issue is still open (`gh issue view N --json state`), or if it names a non-issue blocker that still holds (an ADR still Proposed).
2. **Worktree.** The ticket runs in `../ai-trainer-<n>` on `<area>/<n>-<slug>`, with its own database `ai_trainer_wt<n>`. One Claude tab per ticket, all in the main VS Code window (ADR-0035, #366).
   - **Already in that worktree** (`git branch --show-current` is the ticket's branch): continue.
   - **In the main checkout (`main`):**
     1. Run `make worktree-prune`. It removes worktrees whose PR is merged and keeps the rest, with a line for each. Report those lines in one sentence.
     2. If `git worktree list` shows `../ai-trainer-<n>`, enter it with `EnterWorktree` (`path`, absolute).
     3. Otherwise:
        - if `make worktree` reports Postgres unreachable, run `docker compose up -d --wait db` and retry once;
        - run `make worktree issue=<n>`;
        - enter the printed path with `EnterWorktree`.
     4. If the tab's model doesn't match the ticket's `model:` label, say so in one line and carry on. A running tab can't switch its own model.
   - **Anywhere else:** stop and say where you are.
   - **Inside the worktree, keep shell commands plain and separate.** The session refuses compound commands it can't verify stay in the worktree (a heredoc chained with `gh` was refused at #366). Write files with the Write tool, then run the command on its own.
3. **Ground.** Read the current PRD and the requirement IDs the ticket cites, every referenced ADR and research document, the matching `.claude/rules/`, and the area branch below. If the ticket contradicts an Accepted ADR or the Approved PRD, or needs an undocumented decision, put a doc proposal into the plan — never code around it.
4. **Plan.** Write `.acceptance/<n>/plan.md`:
   - effort S / M / L;
   - the AC → test mapping, one named test per AC;
   - file-level steps and any docs to touch;
   - **UI: yes/no**. Yes when the diff will touch `src/ai_trainer/web/templates/`, `src/ai_trainer/web/static/`, a route that renders HTML, or runs `/implement-design`.

   If the change touches `migrations/`, add a **data-safety** paragraph (ADR-0024): expand or contract, what happens to existing rows, and which backfill.

   Post the plan on the issue with `gh issue comment <n> --body-file .acceptance/<n>/plan.md`, so the owner can stop it later.
5. **Plan critic.** Spawn the `plan-critic` agent with the issue number and the plan path. Pass `model: opus` when the ticket is `risk:high`; otherwise it runs on its default (Sonnet).
   - **PASS** → step 7.
   - **FAIL** → fix the failed items and run it once more. A second FAIL goes to the human plan gate with both verdicts.
6. **HUMAN GATE (plan)**, only when one of these holds:
   - the ticket is `risk:high`;
   - the critic failed twice;
   - the plan carries a doc proposal;
   - the plan carries a migration contract step, which needs the human's explicit yes (ADR-0024);
   - the size is L.

   Use `AskUserQuestion`: Approve / Split / Request changes / Cancel, with the critic's verdict alongside. No product code before Approve. On L or Split, stop and ask the user to run `/create-tickets` for the pieces.
7. **Start.** `gh issue edit <n> --add-label status:in-progress --remove-label status:todo`.
8. **Tests first.** One failing test per AC (`/run-tdd`).
9. **Implement against the docs only.** `/run-tdd` → `/coverage` → ruff → mypy. Regenerate the OpenAPI snapshot if HTTP changed. Run `/tune-prompt` if a prompt, model ID or the router changed. Commits on the ticket branch are fine from here on; never on `main`.
10. **Verify** every AC with evidence: test names, command output, a browser check for web tickets, the smoke run for llm tickets.
    - A visual/design check (`/implement-design`'s parity screenshots, or any other Playwright screenshot comparison) is never self-certified. Its screenshots are saved under `.acceptance/<n>/` and a human confirms the match at the acceptance gate (ADR-0019).
    - Then commit, write the diff with `git diff origin/main...HEAD > .acceptance/<n>/diff.patch` and the AC list to `.acceptance/<n>/acs.md`, and spawn **one** `ac-skeptic` agent with both paths. It tries to disprove each "this AC is done" claim from the diff and the AC text alone.
    - Fix every AC it successfully challenges. The challenge and its fix go in the PR body.
11. **HUMAN GATE (acceptance)** — **UI tickets only** (the plan's `UI: yes`, or the diff touched templates, static files or a rendered route). A human tries the change before the PR opens (ADR-0001 invariant 10, ADR-0035 invariant 4). Never self-certify it.
    - **Stack.** One `make start` serves one worktree at a time on :8000 (ADR-0035). The agent runs it.
      1. Run `make stack-owner`. If it prints `other <path>`, ask with `AskUserQuestion` (Replace / Wait) before running `make stop`. That stack is another tab's acceptance gate.
      2. Run `make start` as a background task in this worktree. It serves this branch against `ai_trainer_wt<n>` and reloads code, templates and CSS live.
      3. Poll `make health` until it answers. Seed the dev user once per worktree database with `uv run python scripts/dev_session.py`.
      4. A new migration or dependency is picked up only at start, so restart the background task after one.
      5. After the gate passes, run `make stop`.

      Never run `make up` alongside it: they use the same ports.
    - **Script.** In the gate message, write a numbered step-by-step click-through, one section per AC or behavior. Start it with "hard-reload first (⌘⇧R, or DevTools → Network → Disable cache)": the live CSS rebuild can leave the browser on a stale `app.css`. Each section gives:
      - the **account**: which one and how to get it into the needed state (an admin email from `ADMIN_EMAILS`, a second Google account left `pending`, or the seeded `scripts/dev_session.py` user, seeded once per worktree database);
      - the **start URL**, as a full `http://localhost:8000/…` link;
      - **each action** spelled out ("click *Continue with Google*", "type `…` in the composer and press Enter");
      - the **expected result** after each step, with the viewport (390×844 phone or desktop) and theme where layout matters.

      At least one section exercises an edge or error case from the AC.
    - **Screenshots.** When the ticket saved any:
      - list every file as a clickable link under `.acceptance/<n>/`, paired mock | app per viewport and theme;
      - give `open .acceptance/<n>` to browse the folder;
      - include the probe table.
    - **Ask.** Make one `AskUserQuestion` call with:
      - the click-through question: Pass / Fail / Blocked;
      - when screenshots exist, "Screenshots match the mock": Approve / Request changes.
    - **Loop.** On Fail, Blocked or Request changes, ask what went wrong if the answer doesn't say. Fix it (step 9), re-verify (step 10) and run this gate again with an updated script. Record every round.
    - Keep `.acceptance/<n>/` after the gate, and never commit it.

    **Non-UI tickets** skip this gate. They instead write an **Acceptance** section for the PR: each command a human can run, with its captured output.
12. **Automated review.** Run `/code-review medium` on the branch. Never pass `--comment`: agents post with the owner's token. Fix what holds. Record each finding with its outcome (fixed / not a bug, and why) for the PR body.
13. **Finish**, all on the ticket branch:
    - If stack or workflow changed, run `/update-docs` so the doc change ships in the same PR.
    - If the ticket changed how to build, run or operate the app, update `README.md` and the `Makefile` in the same change (`.claude/rules/operability.md`).
    - `git fetch origin && git rebase origin/main`. This is the only rebase, before the first push.
    - Then:
      - `uv run alembic heads` must show one head;
      - regenerate the OpenAPI snapshot if HTTP changed;
      - `make check`.
    - Commit with a message referencing `#<n>`, then `git push -u origin <area>/<n>-<slug>`.
    - `gh pr create --draft --base main --title "<issue title> (#<n>)" --body-file <file>`. The body holds:
      - the summary;
      - the AC → evidence table;
      - the plan critic's verdict (and the human plan answer, if asked);
      - the skeptic's challenges and their fixes;
      - the `/code-review` findings with their outcomes;
      - an **Acceptance** section: the script, the answer and any failed rounds for a UI ticket, or the commands and their output otherwise;
      - the data-safety paragraph with the squawk and upgrade-with-data results when `migrations/` changed (ADR-0024);
      - `Closes #<n>`.
    - `gh issue edit <n> --add-label status:in-review --remove-label status:in-progress`.
    - Report the PR URL. The owner reviews the draft PR on GitHub, marks it ready and merges it: that is the review gate (ADR-0035). Ask them to reply **"merged"** in this tab once it's merged. See **Cleanup** below.
    - **Suggest the next tickets, unasked.** Read-only:
      1. List the candidates with `gh issue list --state open --label status:todo --json number,title,labels,body`.
      2. List the work in flight with `gh issue list --state open --label status:in-progress` and `status:in-review`.
      3. A candidate is **ready** when every `Blocked by` issue is closed (`gh issue view N --json state`) and no non-issue blocker holds.
      4. It is **parallel-safe** when, compared with every ticket in flight:
         - its `Touches` line names no shared module;
         - it adds no migration while another in-flight ticket does;
         - neither takes an ADR or PRD number.
      5. List every ready, parallel-safe ticket, unblocking ones first, then smallest. For each give:
         - its `model:` label with one sentence of why (ADR-0035 routing);
         - the line to paste: open a new Claude tab → pick <model> → `/apply-ticket <n>`.

      When nothing is ready, say so, list what blocks each ticket, and name what the human can do (accept an ADR, merge a PR). Never start one.
    - Stop. Never merge the PR yourself, and never commit to, push to or merge into `main`.

## Cleanup (the owner replies "merged")

1. Check `gh pr view <pr> --json state`. If it isn't `MERGED`, say its state and stop.
2. `ExitWorktree` with `action: keep`. It never removes a worktree that `make worktree` created.
3. Back in the main checkout:
   - `git pull --ff-only` on `main`;
   - `make worktree-prune`, which removes this worktree, `ai_trainer_wt<n>` and the branch.
4. Report what was removed.

A tab closed before this step loses nothing. The next `/apply-ticket` run prunes merged worktrees first (step 2).

## Review mode (`/apply-ticket <n> --review`)

Run in a fresh Claude tab after the owner left review comments on the PR. Enter the ticket's worktree first, as in step 2.

1. Find the PR with `gh pr list --head <area>/<n>-<slug> --json number,url`. Read its conversation with `gh pr view <pr> --comments`, and its inline threads with `gh api repos/{owner}/{repo}/pulls/<pr>/comments`.
2. List each comment with what you'll do about it.
   - An ambiguous one becomes an `AskUserQuestion` choice card.
   - One that asks for a decision no doc records becomes an `/update-docs` proposal.
3. Fix with `/run-tdd`, then `/coverage`, ruff and mypy.
4. `git fetch origin && git merge origin/main`. **Never rebase after the first push**: that would need a force push, which is denied (ADR-0035). Then:
   - `uv run alembic heads` must show one head;
   - regenerate the OpenAPI snapshot if HTTP changed;
   - run `make check`;
   - re-run the acceptance gate if a UI behavior changed.
5. Commit referencing `#<n>` and `git push`.
6. Post **one** summary comment with `gh pr comment <pr> --body-file <file>`: each thread, what changed (commit SHA) or why not. Don't reply thread by thread; the comments would appear as the owner's.

## Area branches

- **backend** — layering (ADR-0003), user scoping (ADR-0004), migrations; use `/add-endpoint` for HTTP routes.
- **web** — htmx 4 + daisyUI (ADR-0012, ADR-0019); build designed screens from their Claude Design mock with `/implement-design`, whose Playwright parity screenshots (390×844 and 1440×900, dark and light) are saved under `.acceptance/<n>/parity/` and confirmed by a human at the acceptance gate, not by the agent's own read of them. Verify other behavior ad hoc with the Playwright MCP tools against the running app; add a pytest-playwright spec under `tests/e2e/` only for a flow critical enough to guard against regression forever, not by default (ADR-0013).
- **llm** — templates and evals (ADR-0008, ADR-0034); `TestModel` in tests; the smoke run is evidence.
- **knowledge** — ADR-0011; ingestion details wait for the M3 ADR.
- **e2e** — committed specs against the running compose stack; external services mocked at the API boundary; signed-in pages through the seeded session from `scripts/dev_session.py`; behavior, not pixels.
- **infra** — compose, Dockerfile, CI; verify with `make start` / `docker compose up` and a CI run; the Acceptance section is the commands and their output.
- **docs** — `/update-docs`; no code.

If the area label doesn't match the work, say so in the plan and ask.

## Anti-patterns

- Coding before the plan critic passes, or before the human plan gate when one is required.
- Working in the main checkout instead of the ticket's worktree.
- Asking the owner to open a new VS Code window, run `make worktree` or clean up by hand.
- Self-certifying the acceptance gate, or opening the PR of a UI ticket before that gate passes.
- An acceptance script that says "check the page works" instead of naming each click and its expected result.
- Opening a non-draft PR.
- Posting `/code-review` findings or per-thread replies as PR comments.
- Rebasing or force-pushing after the first push.
- Committing to, pushing to or merging into `main`; merging your own PR.
- Skipping the PRD or ADRs because the ticket "seems clear".
- Implementing blocked work.
- Silently changing the locked stack.
- Creating or pushing a tag, approving a deployment, or connecting to the production host (ADR-0023, ADR-0024).
