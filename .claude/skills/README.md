# Skills: structured workflows

Skills are step-by-step checklists for complex, recurring tasks. Invoke with `/skill-name` or pass arguments like `/skill-name arg1 arg2`.

| Skill | Purpose | When to use | Run with |
|---|---|---|---|
| [update-docs](update-docs/SKILL.md) | Add or revise PRDs and ADRs | Writing product or architecture docs; need to supersede old docs | `/update-docs [prd\|adr] [title]` |
| [create-tickets](create-tickets/SKILL.md) | Scaffold GitHub issues from a template | Planning work for a milestone (e.g., M0, M1) | `/create-tickets <scope>` |
| [apply-ticket](apply-ticket/SKILL.md) | Implement a single GitHub issue end-to-end | Delivering a ticket; includes plan gate and review gate | `/apply-ticket <issue#>` |
| [tune-prompt](tune-prompt/SKILL.md) | Eval-driven change to a prompt, model or router | Changing LLM behavior; includes baseline comparison and human gate | `/tune-prompt <template-id>` |
| [add-endpoint](add-endpoint/SKILL.md) | Checklist for adding or changing a web route | Adding a page, form, API endpoint, or SSE stream | `/add-endpoint` |
| [add-mcp-tool](add-mcp-tool/SKILL.md) | Add a tool or resource to the MCP server | Exposing app behavior to Claude or other agents | `/add-mcp-tool <tool-name>` |
| [add-sport](add-sport/SKILL.md) | Add a new sport as a plugin | Extending training domain without touching core (ADR-0006) | `/add-sport <sport-key>` |
| [coverage](coverage/SKILL.md) | Raise line coverage to 100% in touched modules | After tests pass, before committing | `/coverage` |
| [run-tdd](run-tdd/SKILL.md) | Test-first development loop | When coding features with TDD (red → green → refactor) | `/run-tdd` |

## Non-invokable references

**Library lookups:** There is no skill for this. When you need to look up a library, framework, or SDK API, the [context7 rule](../rules/context7.md) applies automatically — it tells you to use Context7 MCP instead of training data. See the rule for the workflow.

## Execution gates

- **Plan gate:** Use `EnterPlanMode` (via `/plan` or skill instructions) to design before coding. No product code without Approve.
- **Review gate:** Run `/code-review` (or `/code-review ultra` for deeper multi-agent review) before merging. No commit without Approve.

## Adding a new skill

Set `disable-model-invocation: true` in the frontmatter when the skill writes to GitHub, merges or pushes code, or spends money on real model calls (see `create-tickets`, `apply-ticket`, `tune-prompt`) — anything that must wait for an explicit `/skill-name` rather than Claude's own judgment that the description matches. Keep `SKILL.md` under 500 lines; move detail to `scripts/`, `references/`, or `assets/` only once a skill actually needs it.

## Related

- **Rules** (path-based coding standards): [../rules/README.md](../rules/README.md)
- **Main instructions**: [../../CLAUDE.md](../../CLAUDE.md)
- **Ticket templates**: [../../docs/templates/README.md](../../docs/templates/README.md)
