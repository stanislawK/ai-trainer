# ADR 0035 — Parallel agent delivery

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-30 |
| Related | PRD-0009 (G12, G13), ADR-0001 (supersedes invariants 4 and 10 and the three-gate loop in part), ADR-0013, ADR-0019 (the parity gate stays for UI tickets), ADR-0023, ADR-0024 (the contract yes stays a human plan gate) |

## Context

ADR-0001 runs every ticket through three blocking human gates in chat: plan, acceptance and review. Every ticket also shares one checkout, one `make start` stack and one Postgres database, which tests use too. So only one agent works at a time.

Baseline, 2026-10-30, from the session transcripts (`~/.claude/projects/…/*.jsonl`, `usage` per message):

| Ticket | Model | Session start → commit | Main session tokens (new / cache read / out) | Subagent tokens (new / cache read / out) |
|---|---|---|---|---|
| #333 | Opus | 8 min | 55k / 1.39M / 12k | 90k / 297k / 3k |
| #331 | Opus | 9 min | 86k / 2.44M / 25k | 164k / 991k / 11k |
| #310 | Sonnet | 7 min | 68k / 2.39M / 21k | 155k / 565k / 13k |
| #346 | Sonnet | 7 min | 76k / 1.90M / 19k | 71k / 310k / 3k |
| #329 | Sonnet | 6 min | 59k / 1.62M / 13k | 97k / 451k / 6k |
| #326 | Sonnet | 6 min | 51k / 1.21M / 10k | 88k / 247k / 5k |

All six are S-sized refactors. Agent time is short. Sessions left open across the owner's breaks ran for hours instead: #319 22.7 h, #325 4.2 h. So throughput is limited by the owner being needed in a serial chain, not by the agent.

Anthropic's guidance points the same way:
- Parallelize independent tasks, not one tightly coupled task. Most coding has "fewer truly parallelizable tasks than research" ([multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)).
- Route by difficulty: small models for easy inputs ([building effective agents](https://www.anthropic.com/research/building-effective-agents)).
- Give unattended work a pass/fail check, and have a person approve the result at the end ([Claude Code best practices](https://code.claude.com/docs/en/best-practices)).

The owner works in the Claude Code VS Code extension, where every tab in a window shares one folder. So one ticket means one git worktree, opened in its own window.

## Decision

**Routing.** `/create-tickets` gives every ticket two labels, and the next-ticket suggestion repeats them.
- `model:sonnet` or `model:opus`:
  - **Opus** for `risk:high`, size M across three or more modules, a new sport plugin, a cross-cutting refactor, or `/tune-prompt` work;
  - **Sonnet** for everything else.
- `risk:low` or `risk:high`. `risk:high` is any ticket touching:
  - `migrations/`;
  - auth or sessions, admin rights, or tenancy;
  - an ADR or PRD change;
  - the router, the decision gateway, or a prompt or model setting;
  - deploy or release;
  - `.claude/settings.json` or `.claude/hooks/`.

**Plan step.**
1. The agent writes `.acceptance/<n>/plan.md` and posts it as an issue comment, so the owner can stop it later.
2. A read-only `plan-critic` subagent (`.claude/agents/`) reviews it: Sonnet by default, Opus for `risk:high`. It answers PASS only when this closed checklist holds:
   - every AC maps to a named test;
   - the cited ADRs and the `CLAUDE.md` non-negotiables hold;
   - a data-safety paragraph is present when `migrations/` is touched;
   - the size is at most M;
   - no undocumented decision is made.
3. One re-check is allowed after a fix.
4. The human plan gate (ADR-0001) remains for:
   - `risk:high`;
   - a second failed check;
   - a doc conflict;
   - any migration contract step (ADR-0024).

**Acceptance step.**
- **Tickets that change what a person sees** (templates, static files, a rendered route, `/implement-design`) keep ADR-0001's acceptance gate unchanged, including ADR-0019's parity screenshots.
- **Every other ticket** skips the gate. The PR's Acceptance section gives each command with its captured output.

**Review step.** Before the first push:
1. One read-only `ac-skeptic` subagent checks every AC against `.acceptance/<n>/diff.patch`.
2. `/code-review medium` runs locally, with no `--comment`. Agents use the owner's token, so comments would appear as the owner's.
3. Rebase on `origin/main`, check `uv run alembic heads` shows one head, regenerate the OpenAPI snapshot, and run the full checks.
4. Then push and open a **draft** PR. Its body holds the AC → evidence table, the critic verdict, every finding with its outcome, and the Acceptance section.

After the first push:
- Update from `origin/main` by merging, never by rebasing (no force push).
- Handle PR comments with `/apply-ticket <n> --review`, which posts one summary comment.
- The owner's review of the draft PR is the review gate: they mark it ready and merge it.

**Parallel lanes.**
- **One ticket per window:** a ticket runs in `../ai-trainer-<n>`, created by `make worktree issue=<n>` and opened in its own VS Code window.
- **One database per worktree:** each worktree has its own `ai_trainer_wt<n>`, used by its tests and by its `make start`.
- **The shared stack:** the compose project is pinned to `ai-trainer`, so one `make start` serves one worktree at a time on :8000.

### Invariants

1. Only a human merges into `main`. Agents never commit on `main`, push to it, force-push, or run `gh pr merge`. Branch rules, deny rules and the guard hook all enforce this.
2. An agent opens a PR without waiting for a human only as a draft, and only after the skeptic, `/code-review` and the full checks have run.
3. A `risk:high` ticket, a migration contract step and a doc conflict always reach the human plan gate.
4. A ticket that changes what a person sees never reaches its PR without the human acceptance gate (ADR-0001 invariant 10, ADR-0019).
5. At most one ticket that adds a migration is in flight at a time. ADR and PRD numbers are taken only on `docs/*` branches.
6. A worktree's tests and its `make start` use that worktree's own database, never another checkout's.
7. One ticket is never split across concurrent agents; parallelism is between tickets.

## Alternatives considered

- **Drop the plan gate outright:** the reviewer can't judge intent, and Anthropic says human review stays necessary for fit with the wider system ([building effective agents](https://www.anthropic.com/research/building-effective-agents)). Posting the plan to the issue keeps an asynchronous veto.
- **Open-ended evaluator-optimizer critic:** "a reviewer that's asked to find problems will always find *something*" ([best practices](https://code.claude.com/docs/en/best-practices)). A closed checklist with a PASS rule is used instead.
- **Agents as Claude Code teammates, or headless `claude -p` fan-out:** both are CLI-first, and teammates have no worktree isolation ([agent teams](https://code.claude.com/docs/en/agent-teams)). The owner works in the VS Code extension.
- **The extension's own worktrees (`EnterWorktree`, subagent `isolation: worktree`):** they branch under generated names and can't create the database without a `WorktreeCreate` hook, and that hook disables `.worktreeinclude` ([worktrees](https://code.claude.com/docs/en/worktrees)).
- **A port and database offset per worktree for the whole stack:** Google OAuth admits only `http://localhost:8000/auth/callback`. One shared :8000 stack, served to one worktree at a time, keeps sign-in working.

## Consequences

**Rollout, one merge at a time.** Each step needs the one before it to have merged. The skill text changes last, so it never asks for tooling that hasn't shipped yet.
1. #350 adds the local guard: deny rules and the hook.
2. This ADR is accepted.
3. #351 adds `make worktree`, the pinned compose name, `APP_DB_NAME`, and the ignore files.
4. A docs PR edits `apply-ticket`, `create-tickets`, `implement-design`, `tune-prompt`, `rules/web.md`, `rules/llm.md`, `docs/templates/ticket.md`, `.claude/skills/README.md` and `CLAUDE.md`, and adds `.claude/agents/plan-critic.md` and `ac-skeptic.md`.
5. A two-ticket dry run, then an allowlist covering only the permission prompts that actually came up.

**Propagation.** ADR-0001 invariant 3 normally wants that propagation in this change. It is deferred to step 4 at the owner's direction (plan approved 2026-10-30), because the new `apply-ticket` would otherwise require `make worktree` before step 3 ships it. Until then `CLAUDE.md` lists this ADR as Proposed, and the ADR-0001 gates still apply.

**Guards that already exist.** `main` was hardened in #215 (ADR-0023): direct pushes are refused, and the only bypass is "for pull requests only". Agents can't read rulesets or branch protection, because the guard hook denies those APIs. The owner checks the rule.

**Claude Code facts this rests on**, read 2026-10-30:
- Auto mode allows pushes to the default branch and blocks force pushes ([permission modes](https://code.claude.com/docs/en/permission-modes)).
- Deny rules apply in every mode ([permissions](https://code.claude.com/docs/en/permissions)).
- Subagent frontmatter supports `tools`, `model` and `omitClaudeMd` (v2.1.271+) ([subagents](https://code.claude.com/docs/en/sub-agents)).

**Flagged.** Whether agents in the VS Code extension start in auto mode depends on the owner's user settings. Step 5 records the prompts that actually come up.

**Measuring.** After step 5, compare tickets with the table above, using the same transcript sums plus time from `status:in-progress` to the PR opening.
