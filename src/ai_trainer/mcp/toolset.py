"""How a Pydantic AI specialist uses the server in process (ADR-0010, `docs/research/mcp.md`
R4, R5): `MCPToolset(server)` with no network hop, and a `process_tool_call` hook that sends
the run's user in the request meta."""

from typing import Any, Protocol
from uuid import UUID

from fastmcp import FastMCP
from pydantic_ai import RunContext
from pydantic_ai.mcp import CallToolFunc, MCPToolset
from pydantic_ai.mcp import ToolResult as McpCallResult

from ai_trainer.mcp.identity import USER_ID_META_KEY, MissingUserError


class UserDeps(Protocol):
    """The deps of a specialist that uses MCP tools: the user from the authenticated
    session, or `None` when the run has none."""

    @property
    def user_id(self) -> UUID | None: ...


async def inject_user(
    ctx: RunContext[UserDeps],
    call_tool: CallToolFunc,
    name: str,
    tool_args: dict[str, Any],
) -> McpCallResult:
    """Sends the call with the deps' user in meta. With no user it raises before the call,
    which stops the run: the model gets no retry to work around a missing identity
    (ADR-0010)."""
    user_id = getattr(ctx.deps, "user_id", None)
    if not isinstance(user_id, UUID):
        raise MissingUserError
    return await call_tool(name, tool_args, metadata={USER_ID_META_KEY: str(user_id)})


def build_toolset(server: FastMCP) -> MCPToolset[UserDeps]:
    # `tool_error_behavior` stays at its default 'retry', so the model can correct
    # schema-invalid arguments (R3).
    return MCPToolset(server, process_tool_call=inject_user)
