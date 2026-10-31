"""Where an MCP tool gets its user (ADR-0010, `docs/research/mcp.md` R4).

In process, the host's `process_tool_call` hook puts the user ID from the run's deps into the
request meta under `USER_ID_META_KEY`, and a tool reads it back only through
`current_user_id`. Meta is trusted only in process: once HTTP is built, only verified token
claims count."""

from uuid import UUID

from fastmcp.server.context import Context

USER_ID_META_KEY = "user_id"


class MissingUserError(Exception):
    """A tool call carries no usable user. Its message names no user and no athlete data,
    since it can reach the model and traces (ADR-0018)."""

    def __init__(self) -> None:
        super().__init__("no authenticated user for this tool call")


def current_user_id(ctx: Context) -> UUID:
    """The user the host sent with this call. Raises `MissingUserError` when meta carries none
    or something that isn't a UUID, so a call that bypassed the host's hook fails closed."""
    # A plain dict or None on FastMCP 4; it may also hold protocol keys such as progressToken.
    meta = ctx.request_context.meta if ctx.request_context is not None else None
    raw = meta.get(USER_ID_META_KEY) if meta else None
    if not isinstance(raw, str):
        raise MissingUserError
    try:
        return UUID(raw)
    except ValueError:
        raise MissingUserError from None
