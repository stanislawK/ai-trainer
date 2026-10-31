---
paths:
  - "src/ai_trainer/mcp/**"
---

# MCP server and tool rules (ADR-0010, [docs/research/mcp.md](../../docs/research/mcp.md))

ADR-0010 owns the decisions, and `.claude/rules/llm.md` applies here too. Add a tool with `/add-mcp-tool`. These rules carry the research findings, and R-numbers point at its recommendations.

- **Version:** FastMCP 4 (`fastmcp>=4.1,<5`), and `fastmcp` stays a direct dependency because `pydantic-ai-slim[mcp]` installs only the client. Look up its API through context7 at the locked version. FastMCP 3's examples differ.
- **Names (R1):** `snake_case`, no dots, one purpose per tool. Keep reads and draft writes as separate tools.
- **Descriptions (R1):** the docstring, at least 3–4 sentences: what it does, when to use it and when not, what each parameter means, its limits. It isn't a prompt template (ADR-0010).
- **Results (R1):** every list-returning tool takes a bounded `limit` and says when its result was cut. Return names the model can use, plus the IDs a follow-up tool needs, and no internal fields.
- **Name clash (R1):** import our `ToolResult[T]` from its own module. `fastmcp.tools` and `pydantic_ai.mcp` each have their own `ToolResult`, so never import either next to ours.
- **Tool count (R2):** 15 at most, and scope tools per specialist (ADR-0008) before adding more.
- **Server (R3):** build it with `mask_error_details=True`, and keep `MCPToolset`'s `tool_error_behavior` at its default `'retry'`. `'error'` also fails the run on bad arguments, which the model could have corrected.
- **Writes and budgets (R3):** `'retry'` re-calls a tool after an unexpected failure too, so a write tool must be safe to call twice, and a budgeted lookup must not charge twice for one result.
- **Domain failures (R3):** return `success=False` with a concrete `recovery_hint`. Never raise for these: a raise uses up the retries, then fails the run. Another user's row reads as not found from a user-scoped query, which is one of these.
- **Identity (R4):** guard it twice.
  - The `process_tool_call` hook raises an application exception when deps carry no user, and sends the rest with `call_tool(name, args, metadata={...})`. `metadata` is keyword-only.
  - `current_user_id(ctx)` reads `ctx.request_context.meta` with `.get` (a plain `dict` or `None`) and raises when the user is missing.
- **Raises aren't protocol errors (R3a):** FastMCP turns a raise inside a tool into an `isError` result, which `MCPToolset` sends back to the model by default. So the host's hook is what keeps the model from retrying around identity (ADR-0010).
- **Resources:** the hook wraps tool calls only. Don't add a user-scoped resource until its ticket has settled and tested how identity reaches it (the research document's Open questions).
- **Exception messages (R4):** no athlete data, because they can reach the model and traces (ADR-0018).
- **Telemetry:** FastMCP's spans are on by default and record once an SDK is configured, which this project does (ADR-0018). A test with an in-memory exporter proves no span carries the user ID or athlete text (#378).
- **Era (R5):** `Client` negotiates the sessionless `2026-07-28` era by default, so don't rely on `ctx.set_state`, `on_initialize` or `ctx.elicit()`. Ambiguity is a choice card (ADR-0015).
- **Tests (R6):** ADR-0010's four through `Client(server)`, where missing identity shows as `ToolError` from `call_tool`. Add one `TestModel` run through the real hook, and one proving schema-invalid arguments leave the run recoverable.
