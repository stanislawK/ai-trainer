---
name: add-mcp-tool
description: Checklist for adding an MCP tool or resource to the FastMCP server, with user identity from context and draft-only writes.
argument-hint: "[tool-name]"
---

# Add MCP tool: $ARGUMENTS (ADR-0010)

0. Read [docs/research/mcp.md](../../../docs/research/mcp.md) and [.claude/rules/mcp.md](../../rules/mcp.md), and follow them. Look up FastMCP 4's API through context7 at the locked version.
1. Put the tool in `src/ai_trainer/mcp/` and make it call one application use case; no business logic in the tool. Name it in `snake_case` with no dots, one purpose per tool, and check the server stays at 15 tools or fewer (ADR-0010). A user-scoped resource waits until its ticket has settled how identity reaches it (the research document's Open questions).
2. Resolve the user only with `current_user_id(ctx)`. Never add a `user_id` parameter.
3. Return `ToolResult[T]`, parameterized with the tool's own typed Pydantic output model (never `dict[str, Any]`). On a domain failure (bad input, not found, empty result, spent lookup budget) return `success=False` with a concrete `recovery_hint` telling the caller how to retry — don't raise for these. A tool returning a list takes a bounded `limit` and says when its result was cut. Return names the model can use, plus the IDs a follow-up tool needs, and no internal fields.
   The docstring is the model-facing description: at least 3–4 sentences on what it does, when to use it and when not, what each parameter means, and its limits. Exception messages carry no athlete data.
4. A write tool creates a draft or a proposal only; the user confirms it in the UI. It must be safe to call twice, because an unexpected failure is retried.
5. Tests with FastMCP's in-memory `Client(server)`: a happy path; a tenancy check (identity for user A sent through `call_tool(..., meta=...)` never returns user B's data); missing identity → `call_tool` raises `ToolError`, never a `ToolResult`; a domain failure → `ToolResult(success=False, recovery_hint=...)`; once HTTP is built (deferred in ADR-0010), meta is ignored there and only the token counts.
   If the tool is the first of its kind in a toolset, add a `TestModel` run through the real `process_tool_call` hook, and a check that schema-invalid arguments leave the run recoverable.
6. Before wiring the tool into a specialist, sanity-check it manually with the MCP Inspector (`npx @modelcontextprotocol/inspector`) — confirms the generated JSON schema and both `ToolResult` branches look right, faster than debugging through an agent conversation.
7. Add the tool to the toolsets of the specialists that need it (ADR-0008). If it changes agent behaviour, extend those templates' eval datasets and run `/tune-prompt`.
