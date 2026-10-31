"""The FastMCP server the app's specialists use in process (ADR-0010). Each tool calls one
application use case and holds no business logic; its docstring is the description FastMCP
publishes."""

from datetime import date
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.server.context import Context
from pydantic import Field

from ai_trainer.application.training_history import (
    MAX_SESSIONS,
    SessionPage,
    SessionQuery,
    SessionQueryFailed,
    TrainingHistory,
)
from ai_trainer.mcp.identity import current_user_id
from ai_trainer.mcp.results import ToolResult


def build_mcp_server(history: TrainingHistory) -> FastMCP:
    # Only a ToolError's message reaches the client; any other exception becomes a generic
    # one, so an unexpected failure can't leak athlete data (R3).
    server = FastMCP("ai-trainer", mask_error_details=True)

    @server.tool
    async def query_sessions(
        ctx: Context,
        sport: str | None = None,
        since: date | None = None,
        until: date | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_SESSIONS)] = 10,
    ) -> ToolResult[SessionPage]:
        """List the athlete's own saved training sessions, the latest day first, with each
        session's ID, sport, day, duration in minutes, RPE, feeling and notes. Use it to look
        back at what the athlete trained, for example before judging load or answering "what
        did I do last week"; don't use it for drafts that are not confirmed yet, or for the
        exercises inside a session. `sport` is a sport ID such as `climbing`, `gym` or
        `cycling` and leaves other sports out; `since` and `until` are ISO days (YYYY-MM-DD),
        both included; `limit` is how many sessions to return, 1 to 50, default 10. When more
        sessions match than `limit`, `truncated` is true: narrow the days or raise the limit.
        A query that finds nothing comes back with `success` false and a `recovery_hint`."""
        outcome = await history.query(
            current_user_id(ctx),
            SessionQuery(sport_id=sport, since=since, until=until, limit=limit),
        )
        if isinstance(outcome, SessionQueryFailed):
            return _failed(outcome)
        return ToolResult.ok(outcome)

    return server


def _failed(failure: SessionQueryFailed) -> ToolResult[SessionPage]:
    match failure.reason:
        case "unknown_sport":
            return ToolResult[SessionPage].failed(
                "Unknown sport.",
                f"Call again with one of: {', '.join(failure.known_sports)}, or no sport.",
            )
        case "since_after_until":
            return ToolResult[SessionPage].failed(
                "`since` is after `until`.",
                "Call again with `since` on or before `until`.",
            )
        case "no_sessions":
            return ToolResult[SessionPage].failed(
                "No saved session matches.",
                "Widen the days or drop the sport filter. If nothing comes back without "
                "filters, the athlete has no saved sessions yet.",
            )
