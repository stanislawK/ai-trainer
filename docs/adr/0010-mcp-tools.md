# ADR 0010 — MCP tools

| | |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Related | PRD-0001 (G1, G9, B1, B5, B8–B11, B18, B21, B22), ADR-0005, ADR-0007, ADR-0008, ADR-0014, ADR-0016, ADR-0017 |

## Context

Specialist agents need data: training history, load, knowledge, plans. The project owner wants custom tools and resources over MCP, so the same tools serve the app's agents and external MCP clients.

## Decision

- A standalone **FastMCP 3.x** server in `src/ai_trainer/mcp/`.
- **Inside the app**, Pydantic AI specialists consume it in-process with `MCPToolset(server)` — no network hop.
- **For external MCP clients**, it is mounted into FastAPI with `mcp.http_app(...)`, with its lifespan merged through `combine_lifespans` (required for session management).
- **Starter tools:** `query_sessions`, `training_load_summary`, `search_knowledge`, `get_active_plan`, `propose_plan_change`, `draft_session`, and from M5 `lookup_route`, `recommend_area_routes`, `resolve_place` (ADR-0016, ADR-0017).
- **Resources:** athlete profile, availability, current plan, and `current_datetime` for external clients, which have no deps to inject the date into (ADR-0014).
- Reference-data tools read shared, non-user-scoped tables, so `current_user_id(ctx)` governs their lookup budget and rate limit (B22) rather than the rows they return.
- Every tool calls an application use case; tools contain no business logic.
- **Result envelope:** every tool returns `ToolResult[T]`, one generic Pydantic model (`success: bool`, `data: T | None`, `error: str | None`, `recovery_hint: str | None`) parameterized with the tool's own typed output model — never `dict[str, Any]`. A tool that fails on bad input, an empty result, or a spent lookup budget (B22) returns `success=False` with a `recovery_hint` so the specialist can retry correctly or fall back to a clarification card (ADR-0015), instead of surfacing a raw exception. Identity and tenancy failures are not modeled this way — they raise, becoming a protocol-level MCP error, because they are never something the model should retry around.

**User identity** comes from one helper, `current_user_id(ctx)`:

- *In-process:* the host injects the user ID from `RunContext.deps` into the MCP request meta with `MCPToolset(process_tool_call=…)`, and the tool reads it from `ctx.request_context.meta`.
- *Over HTTP:* only verified token claims count (`get_access_token()`). Meta sent by an HTTP client is ignored, because the client controls it.

### Invariants

1. Tools get `user_id` only through `current_user_id(ctx)` — never as a tool argument.
2. Write tools create drafts or proposals only; the user confirms them in the UI.
3. Tool results are typed Pydantic models, returned as `ToolResult[T]`.
4. Every tool has tests through FastMCP's in-memory `Client(server)`: a happy path, a tenancy check, missing identity (raises), and a domain failure (`success=False` with a `recovery_hint`).

## Consequences

- ⚠ The first MCP ticket proves with a test that `process_tool_call` meta reaches a FastMCP 3 tool in-process.
- ⚠ Practitioner experience shows agent tool selection degrades past roughly 10–20 simultaneously active tools, well under the protocol's limit — keep this server in the 5–15 range. It starts at 6 tools (9 from M5); if later growth threatens that ceiling, split by domain or lean harder on ADR-0008's per-specialist toolsets before adding more tools here.
- The `/add-mcp-tool` skill implements these invariants.
