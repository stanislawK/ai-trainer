# ADR 0010 — MCP tools

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (G1, G9, B1, B5, B8–B11, B18, B21, B22), ADR-0005, ADR-0007, ADR-0008, ADR-0014, ADR-0016, ADR-0017 |

## Context

Specialist agents need data: training history, load, knowledge, plans. The project owner wants custom tools and resources over MCP, so the same tools serve the app's agents and external MCP clients.

## Decision

- A standalone **FastMCP 4** server in `src/ai_trainer/mcp/`: `fastmcp>=4.1,<5`, with the exact version in `uv.lock` (see Consequences).
- **Inside the app**, Pydantic AI specialists consume it in-process with `MCPToolset(server)` — no network hop.
- **For external MCP clients**, later: it is mounted into FastAPI with `mcp.http_app(...)`, with its lifespan merged through `combine_lifespans` (required for session management). Deferred, owner-directed 2026-10-31, until an amendment decides how tokens are issued. The app's Google sign-in uses server-side sessions and issues none (ADR-0005).
- **Starter tools:** `query_sessions`, `training_load_summary`, `search_knowledge`, `get_active_plan`, `propose_plan_change`, `draft_session` (deferred until a specialist needs it, owner-directed 2026-10-31), and from M5 `lookup_route`, `recommend_area_routes`, `resolve_place` (ADR-0016, ADR-0017).
- **Resources:** athlete profile, availability, current plan, and `current_datetime` for external clients, which have no deps to inject the date into (ADR-0014).
- Reference-data tools read shared, non-user-scoped tables, so `current_user_id(ctx)` governs their lookup budget and rate limit (B22) rather than the rows they return.
- Every tool calls an application use case; tools contain no business logic.
- **Result envelope:** every tool returns `ToolResult[T]`, one generic Pydantic model (`success: bool`, `data: T | None`, `error: str | None`, `recovery_hint: str | None`) parameterized with the tool's own typed output model — never `dict[str, Any]`. A tool that fails on bad input, an empty result, or a spent lookup budget (B22) returns `success=False` with a `recovery_hint` so the specialist can retry correctly or fall back to a clarification card (ADR-0015), instead of surfacing a raw exception. Identity and tenancy failures are not modeled this way — they raise, becoming a protocol-level MCP error, because they are never something the model should retry around.

**User identity** comes from one helper, `current_user_id(ctx)`:

- *In-process:* the host injects the user ID from `RunContext.deps` into the MCP request meta with `MCPToolset(process_tool_call=…)`, and the tool reads it from `ctx.request_context.meta`.
- *Over HTTP, once built:* only verified token claims count (`get_access_token()`). Meta sent by an HTTP client is ignored, because the client controls it.

### Invariants

1. Tools get `user_id` only through `current_user_id(ctx)` — never as a tool argument.
2. Write tools create drafts or proposals only; the user confirms them in the UI.
3. Tool results are typed Pydantic models, returned as `ToolResult[T]`.
4. Every tool has tests through FastMCP's in-memory `Client(server)`: a happy path, a tenancy check, missing identity (raises), and a domain failure (`success=False` with a `recovery_hint`).

## Consequences

- ⚠ The first MCP ticket proves with a test that `process_tool_call` meta reaches a FastMCP tool in-process.
- ⚠ Practitioner experience shows agent tool selection degrades past roughly 10–20 simultaneously active tools, well under the protocol's limit — keep this server in the 5–15 range. It starts at 6 tools (9 from M5); if later growth threatens that ceiling, split by domain or lean harder on ADR-0008's per-specialist toolsets before adding more tools here.
- The `/add-mcp-tool` skill implements these invariants.
- Hot path and tools, approved in the gym-exercises plan on 2026-10-30 ([ADR-0031](0031-exercise-catalogue-and-name-resolution.md)):
  - The logging hot path calls its use cases in code, such as resolving exercise names against the athlete's history (B37). A model's tool-call round trip there would break G14.
  - MCP tools wrap the same use cases for specialists and external clients. M1g adds `find_exercise` and `exercise_history`. M3 adds `search_knowledge` ([ADR-0033](0033-knowledge-ingestion-and-admin-upload.md)).
  - Before the first tool, a research ticket writes [docs/research/mcp.md](../research/mcp.md) from trusted sources only, updates `/add-mcp-tool` and adds `.claude/rules/mcp.md`. Tickets that build the server or a tool cite that document (owner-directed 2026-10-31).
- Version, chosen in [#369](https://github.com/stanislawK/ai-trainer/issues/369) from [docs/research/mcp.md](../research/mcp.md) (question 7): FastMCP 4. `pydantic-ai-slim` 2.57 installs it by default and supports 3 alongside it ([Pydantic AI v2.57.0](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md)). It uses `httpx2`, which this project already locks, and what it removes touches nothing planned here ([FastMCP upgrade guide, v4.1.4](https://github.com/PrefectHQ/fastmcp/blob/v4.1.4/docs/getting-started/upgrading/from-fastmcp-3.mdx)). The `mcp` extra installs only the client, so `fastmcp` is a direct dependency ([FastMCP v4.1.3](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/integrations/pydantic-ai.mdx)).
- ⚠ FastMCP 4.0.0 is from 2026-08-31 and releases often ([PyPI](https://pypi.org/project/fastmcp/#history), read 2026-10-31). Take updates deliberately, through `uv.lock`.
- In-process meta is documented for both libraries: `call_tool(name, args, metadata={...})` inside `process_tool_call` ([Pydantic AI v2.57.0](https://github.com/pydantic/pydantic-ai/blob/v2.57.0/docs/mcp/client.md)) and `ctx.request_context.meta`, a plain `dict` or `None` on FastMCP 4 ([FastMCP v4.1.3](https://github.com/PrefectHQ/fastmcp/blob/v4.1.3/docs/servers/context.mdx)). The first server ticket still proves it with a test, on the chosen version.
