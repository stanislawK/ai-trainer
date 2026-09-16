# ADR 0008 — Conversation routing and prompt templates

| | |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Related | PRD-0001 (G2, G3, G5, G6, B1, B3, B14), ADR-0006, ADR-0007, ADR-0009, ADR-0010 |

## Context

A chat message can log a session, ask a question, change a plan, or several of these at once (B14). One large agent prompt is expensive, hard to test and inconsistent. The project owner proposed classifying each message first and then running an intent-specific prompt template.

## Decision

**Pipeline** — a deterministic workflow; agents run only inside specialists:

```
message + recent turns
  → Router (template "router", small model) → list[Intent]
  → Dispatcher: safety first, then message order
  → one specialist per intent (template; tools only where data is needed)
  → outputs combined into one streamed reply
```

**Intents** — a discriminated union on `kind`; every intent carries a confidence (0–1) and the text span it covers:
`log_session(sport)` · `edit_session` · `ask_training_question` · `request_plan` · `adjust_plan` · `request_report` · `update_profile` · `wellbeing_or_injury` · `chitchat` · `unclear`.
The `sport` values are generated from `SportRegistry` (ADR-0006). For `log_session`, the specialist is the sport plugin's extraction template.

**Router rules:** `wellbeing_or_injury` is handled first (G3). `unclear`, or a confidence below the threshold in settings, produces one clarifying question (B3). The router sees the last N turns (N in settings), so follow-ups like "oh, and 2 more 6B" resolve.

**Prompt templates:**

- A typed registry in `src/ai_trainer/llm/prompts/`. Each `PromptTemplate` declares `id`, `version`, `locale` (only `en` in v1), a deps model (the template's variables), a Pydantic output model, and a model settings key.
- Bodies are files at `src/ai_trainer/llm/prompts/<id>/v<N>.<locale>.md`, loaded as Pydantic AI `TemplateStr` (Handlebars-style `{{var}}`). Variable names are validated against the deps model when the agent is built.
- The persona (`src/ai_trainer/llm/prompts/_persona/v<N>.en.md`) is the static instruction of every user-facing agent; the specialist template is added as dynamic instructions.
- Pydantic AI's YAML `AgentSpec` is not used: it describes outputs as JSON schema and would lose the Python output types.
- MCP prompts are user-selected templates in MCP clients, not an internal pipeline step. Selected templates may later be exposed through FastMCP `@mcp.prompt`, generated from this registry.

### Invariants

1. No prompt text in Python string literals outside tests.
2. A template version with a committed eval baseline is never edited in place. A change is a new `v<N+1>` file; the registry selects the active version.
3. A template gets an eval dataset before it becomes active (G6, ADR-0009).
4. The safety intent is always handled before any other intent in the message.
5. The router never writes data; specialists produce drafts and proposals (ADR-0010).

## Consequences

- The `/tune-prompt` skill implements invariants 2–3.
- Chat-history and context-window strategy (how much history is sent, summarisation) is decided in an M1 ADR.
