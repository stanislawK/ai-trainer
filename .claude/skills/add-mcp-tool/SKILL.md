---
name: add-mcp-tool
description: Checklist for adding an MCP tool or resource to the FastMCP server, with user identity from context and draft-only writes.
argument-hint: "[tool-name]"
---

# Add MCP tool: $ARGUMENTS (ADR-0010)

1. Put the tool in `src/ai_trainer/mcp/` and make it call one application use case; no business logic in the tool.
2. Resolve the user only with `current_user_id(ctx)`. Never add a `user_id` parameter.
3. Return `ToolResult[T]`, parameterized with the tool's own typed Pydantic output model (never `dict[str, Any]`). On a domain failure (bad input, not found, empty result, spent lookup budget) return `success=False` with a concrete `recovery_hint` telling the caller how to retry — don't raise for these. The docstring is the model-facing description: say what it does and when to use it.
4. A write tool creates a draft or a proposal only; the user confirms it in the UI.
5. Tests with FastMCP's in-memory `Client(server)`: a happy path; a tenancy check (identity for user A never returns user B's data); missing identity → raises (protocol-level error, not a `ToolResult`); a domain failure → `ToolResult(success=False, recovery_hint=...)`; over HTTP, meta is ignored and only the token counts.
6. Before wiring the tool into a specialist, sanity-check it manually with the MCP Inspector (`npx @modelcontextprotocol/inspector`) — confirms the generated JSON schema and both `ToolResult` branches look right, faster than debugging through an agent conversation.
7. Add the tool to the toolsets of the specialists that need it (ADR-0008). If it changes agent behaviour, extend those templates' eval datasets and run `/tune-prompt`.
