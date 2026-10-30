# Skills: structured workflows

Skills are step-by-step checklists for complex, recurring tasks. Invoke with `/skill-name` or pass arguments like `/skill-name arg1 arg2`.

| Skill | Purpose | When to use | Run with |
|---|---|---|---|
| [update-docs](update-docs/SKILL.md) | Add or revise PRDs and ADRs | Writing product or architecture docs; need to supersede old docs | `/update-docs [prd\|adr] [title]` |
| [create-tickets](create-tickets/SKILL.md) | Scaffold GitHub issues from a template | Planning work for a milestone (e.g., M0, M1) | `/create-tickets <scope>` |
| [apply-ticket](apply-ticket/SKILL.md) | Implement a single GitHub issue end-to-end | Delivering a ticket in its own worktree; a plan critic (human plan gate when risky), the acceptance gate for UI tickets, automated review, a draft PR, then the parallel-safe next tickets; `--review` handles PR comments | `/apply-ticket <issue#> [--review]` |
| [tune-prompt](tune-prompt/SKILL.md) | Smoke-checked change to a prompt, decision set, threshold, model or router (ADR-0034) | Changing LLM behavior; smoke set from real chats, full runs only on demand | `/tune-prompt <template-id>` |
| [implement-design](implement-design/SKILL.md) | Turn a Claude Design mock into daisyUI/Jinja and prove parity with Playwright | Any web ticket that builds or changes a designed screen (ADR-0019) | `/implement-design <screen>` |
| [add-endpoint](add-endpoint/SKILL.md) | Checklist for adding or changing a web route | Adding a page, form, API endpoint, or SSE stream | `/add-endpoint` |
| [add-mcp-tool](add-mcp-tool/SKILL.md) | Add a tool or resource to the MCP server | Exposing app behavior to Claude or other agents | `/add-mcp-tool <tool-name>` |
| [add-decision](add-decision/SKILL.md) | Add a Jev decision set (classify, route, gate, score, closed-set draft field) | A step only picks a label, a yes/no, a level or a draft field's value from a fixed set, and Jev is faster or cheaper than an LLM call (ADR-0020) | `/add-decision <decision-id>` |
| [add-sport](add-sport/SKILL.md) | Add a new sport as a plugin | Extending training domain with the sport's own tables, without touching core (ADR-0006, ADR-0032) | `/add-sport <sport-key>` |
| [coverage](coverage/SKILL.md) | Raise line coverage to 100% in touched modules | After tests pass, before committing | `/coverage` |
| [run-tdd](run-tdd/SKILL.md) | Test-first development loop | When coding features with TDD (red → green → refactor) | `/run-tdd` |

## Non-invokable references

**OpenRouter facts:** There is no skill for this either. For model, upstream, price, benchmark or live-latency questions, the [openrouter-mcp rule](../rules/openrouter-mcp.md) applies: read-only OpenRouter MCP tools, cited with the date.

**Library lookups:** There is no skill for this. When you need to look up a library, framework, or SDK API, the [context7 rule](../rules/context7.md) applies automatically — it tells you to use Context7 MCP instead of training data. See the rule for the workflow.

## Execution gates

How `/apply-ticket` gates a ticket (ADR-0001 as amended by ADR-0035):

- **Plan.** The `plan-critic` agent checks the plan against a closed checklist. A human plan gate (`AskUserQuestion`) is added for `risk:high`, a second failed check, a doc conflict, a migration contract step or an L size. The plan is always posted on the issue. No product code before it passes.
- **Acceptance.** For UI tickets only: a human tries the change on `make start`, served from the ticket's worktree, following the agent's step-by-step script. They also approve any screenshots under `.acceptance/<issue#>/`. The answer is Pass / Fail / Blocked; the agent never self-certifies (ADR-0001). Other tickets put their commands and output in the PR's Acceptance section.
- **Review.** First the `ac-skeptic` agent and `/code-review medium` run locally (never `--comment`). Then the agent opens a **draft** PR. The owner reviews it on GitHub, marks it ready and merges it. `/apply-ticket <n> --review` answers the review comments.

## Agents

`.claude/agents/` holds the read-only subagents the delivery loop spawns:
- `plan-critic`: a closed-checklist gate on the plan;
- `ac-skeptic`: tries to disprove each AC from the diff alone.

Two orchestration shapes recur in the skills: Fan Out & Synthesize (`add-sport`) and Adversarial Verification (`apply-ticket`'s skeptic). Reuse those terms rather than inventing a new shape when editing a skill.

## Adding a new skill

Set `disable-model-invocation: true` in the frontmatter when the skill writes to GitHub, merges or pushes code, or spends money on real model calls (see `create-tickets`, `apply-ticket`, `tune-prompt`) — anything that must wait for an explicit `/skill-name` rather than Claude's own judgment that the description matches. Keep `SKILL.md` under 500 lines; move detail to `scripts/`, `references/`, or `assets/` only once a skill actually needs it.

## Related

- **Rules** (path-based coding standards): [../rules/README.md](../rules/README.md)
- **Main instructions**: [../../CLAUDE.md](../../CLAUDE.md)
- **Ticket templates**: [../../docs/templates/README.md](../../docs/templates/README.md)
