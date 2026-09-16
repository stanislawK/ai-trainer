# ADR 0010 — MCP tools

| | |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Related | PRD-0001 (G1, B1, B5, B8–B11), ADR-0005, ADR-0007, ADR-0008 |

## Context

Specialist agents need data: training history, load, knowledge, plans. The project owner wants custom tools and resources over MCP, so the same tools serve the app's agents and external MCP clients.

## Decision

- A standalone **FastMCP 3.x** server in `src/ai_trainer/mcp/`.
- **Inside the app**, Pydantic AI specialists consume it in-process with `MCPToolset(server)` — no network hop.
- **For external MCP clients**, it is mounted into FastAPI with `mcp.http_app(...)`, with its lifespan merged through `combine_lifespans` (required for session management).
- **Starter tools:** `query_sessions`, `training_load_summary`, `search_knowledge`, `get_active_plan`, `propose_plan_change`, `draft_session`.
- **Resources:** athlete profile, availability, current plan.
- Every tool calls an application use case; tools contain no business logic.

**User identity** comes from one helper, `current_user_id(ctx)`:

- *In-process:* the host injects the user ID from `RunContext.deps` into the MCP request meta with `MCPToolset(process_tool_call=…)`, and the tool reads it from `ctx.request_context.meta`.
- *Over HTTP:* only verified token claims count (`get_access_token()`). Meta sent by an HTTP client is ignored, because the client controls it.

### Invariants

1. Tools get `user_id` only through `current_user_id(ctx)` — never as a tool argument.
2. Write tools create drafts or proposals only; the user confirms them in the UI.
3. Tool results are typed Pydantic models.
4. Every tool has tests through FastMCP's in-memory `Client(server)`: a happy path, a tenancy check and missing identity.

## Consequences

- ⚠ The first MCP ticket proves with a test that `process_tool_call` meta reaches a FastMCP 3 tool in-process.
- The `/add-mcp-tool` skill implements these invariants.
