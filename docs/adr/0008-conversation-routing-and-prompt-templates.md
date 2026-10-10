# ADR 0008 — Conversation routing and prompt templates

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (G2, G3, G5, G6, G9, B1, B3, B14, B17), PRD-0003 (B23, F15), ADR-0006, ADR-0007, ADR-0009, ADR-0010, ADR-0014, ADR-0015 |

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

**Router rules:** `wellbeing_or_injury` is handled first (G3). `unclear`, or a confidence below the threshold in settings, produces a structured `Clarification` — a short list of options to pick from, not a free-text question (B3, B17, ADR-0015). The router and every specialist see the last `chat_history_turns` messages (one setting), so follow-ups like "oh, and 2 more 6B" resolve.

**Sport inference (B23).** The router infers the `sport` of every `log_session` intent and does not ask when it can guess. Its deps carry the athlete's own sports and the sports of their most recent sessions, next to the recent turns. An athlete with one sport gets that sport without a model guess. A sport confidence below its own threshold in settings produces a `Clarification` offering only the athlete's sports that stay plausible, never the full registry. A confident guess goes straight to the extraction specialist; the draft card shows it and the athlete can change it there. Each reply part records the sports it is about, so the UI can draw the reply glyph (F15, ADR-0019).

**Prompt templates:**

- A typed registry in `src/ai_trainer/llm/prompts/`. Each `PromptTemplate` declares `id`, `version`, `locale` (only `en` in v1), a deps model (the template's variables), a Pydantic output model, and a model settings key.
- Bodies are files at `src/ai_trainer/llm/prompts/<id>/v<N>.<locale>.md`, loaded as Pydantic AI `TemplateStr` (Handlebars-style `{{var}}`). Variable names are validated against the deps model when the agent is built.
- The persona (`src/ai_trainer/llm/prompts/_persona/v<N>.en.md`) is the static instruction of every user-facing agent; the specialist template is added as dynamic instructions.
- Every user-facing template's deps model carries `today`, `now_local` and `timezone`, so relative dates resolve without a tool call (ADR-0014).
- Pydantic AI's YAML `AgentSpec` is not used: it describes outputs as JSON schema and would lose the Python output types.
- MCP prompts are user-selected templates in MCP clients, not an internal pipeline step. Selected templates may later be exposed through FastMCP `@mcp.prompt`, generated from this registry.

### Invariants

1. No prompt text in Python string literals outside tests.
2. A template version with a committed eval baseline is never edited in place. A change is a new `v<N+1>` file; the registry selects the active version.
3. A template gets an eval dataset before it becomes active (G6, ADR-0009).
4. The safety intent is always handled before any other intent in the message.
5. The router never writes data; specialists produce drafts and proposals (ADR-0010).

## Consequences

- Owner-directed amendment, 2026-09-23 (PRD 0003, B23 and F15): sport inference and the recorded reply sports. The router eval dataset gains sport-inference cases (single-sport athlete, sport given by units or grades, a sport settled by recent sessions, a truly ambiguous message).

- The `/tune-prompt` skill implements invariants 2–3.
- Owner-directed amendment, 2026-09-29: chat history and the M1 skeleton.
  - **History.** Each user's chat is stored in a user-owned `chat_messages` table. A row holds the role, the text, the sports the reply is about (F15), the template ID and version that produced it, and `created_at`.
  - The router and every specialist get the same last `chat_history_turns` messages. There is no summarisation; a new ADR takes that up once real conversations outgrow the window.
  - **Skeleton routing.** Until each specialist exists:
    - the dispatcher sends `chitchat`, `unclear` and `wellbeing_or_injury` to the `chitchat` specialist. The persona carries G3, the chitchat eval dataset includes safety cases, and invariant 4's ordering still holds;
    - every other intent gets a fixed "not yet" reply rendered from a template file, with no model call;
    - as each specialist ships, it replaces its intent's fallback;
    - `unclear` produces a `Clarification` again once ADR-0015's choice card lands with M1 logging.
- Owner-directed amendment, 2026-10-09 (#81): text output for replies. A template that writes the athlete's reply declares `str` output instead of a Pydantic output model, so the reply can stream as text (F1). The first such template is the `chitchat` specialist. Templates whose output the app reads, such as the router and extraction, keep their Pydantic output model. Its eval dataset grades the text with `LLMJudge` rubrics (ADR-0009).
- Skeleton dispatcher details, approved at #82's plan gate, 2026-10-09:
  - Intents bound for the same specialist share one call. `chitchat`, `unclear` and `wellbeing_or_injury` make one `chitchat` call that sees the whole message, so "my knee hurts, also hi" is one reply with the pain first. Every intent without a specialist shares one "not yet" reply. Parts are ordered safety first, then by message order.
  - Each part is its own `chat_messages` row, with its template ID and version and the sports it is about (the "not yet" part carries its `log_session` sports).
  - The "not yet" text is a fixed reply file, `llm/prompts/not_yet/v<N>.en.md`, read through `PromptRegistry.load_fixed_reply`. It is never sent to a model, so it needs no eval dataset.
  - Nothing is saved until every part came through. A router or specialist failure ends the stream with a retryable error, and a retry streams a fresh reply. A message already answered replays its saved reply with no model call.
  - A reply belongs to the athlete message it follows: its rows come after that message and before the next one, by `created_at`. Each part is stamped one microsecond after what it follows, counted from the message, not from when the reply was saved. A reply that finishes after the athlete has already sent their next message still sorts under the message it answers, and that next message never replays it. The parts are saved together in a task the reader's disconnect can't cancel.
  - Router confidence is not used yet. A low-confidence intent is routed like any other until ADR-0015's choice card brings back the threshold setting.
  - The app sends the model the same instructions the eval runs sent: `PromptRegistry.render_instructions` renders persona and template exactly as `build_agent` does, and the eval CLI loads its templates from the app's `build_prompt_registry`.
- A mixed-tier fan-out inside a specialist (several fast/cheap model calls gathering or summarizing sources in parallel, one stronger model composing the final reply) is worth considering once a multi-source specialist exists (e.g. multi-session report generation) — not decided here; revisit in the ADR that introduces that specialist.
