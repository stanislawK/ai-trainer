"""`scripts/search_knowledge.py` (#424): its arguments, and that it calls the tool with the user
in the request meta, as the specialists' hook does. Its `run()` needs the real database and
OpenRouter, so the PR's Acceptance section runs it instead."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.server.context import Context

from ai_trainer.mcp.identity import current_user_id
from ai_trainer.mcp.server import SEARCH_KNOWLEDGE_TOOL
from scripts.search_knowledge import call_search, parse_args

_USER = UUID("00000000-0000-4000-8000-000000000424")


def _echo_server(seen: list[tuple[UUID, dict[str, Any]]]) -> FastMCP:
    server = FastMCP("echo")

    @server.tool(name=SEARCH_KNOWLEDGE_TOOL)
    async def search_knowledge(
        ctx: Context, query: str, limit: int, sport: str | None = None
    ) -> dict[str, Any]:
        seen.append((current_user_id(ctx), {"query": query, "limit": limit, "sport": sport}))
        return {"success": True}

    return server


def _args(argv: Sequence[str]) -> Any:
    return parse_args(argv)


def test_the_query_is_required_and_sport_and_limit_are_optional() -> None:
    args = _args(["how often to deload"])
    assert (args.query, args.sport, args.limit) == ("how often to deload", None, 5)
    args = _args(["deload", "--sport", "gym", "--limit", "3"])
    assert (args.query, args.sport, args.limit) == ("deload", "gym", 3)


async def test_the_tool_is_called_with_the_user_in_meta() -> None:
    seen: list[tuple[UUID, dict[str, Any]]] = []

    result = await call_search(_echo_server(seen), _USER, _args(["deload", "--sport", "gym"]))

    assert result == {"success": True}
    assert seen == [(_USER, {"query": "deload", "limit": 5, "sport": "gym"})]


async def test_no_sport_sends_no_sport_argument() -> None:
    seen: list[tuple[UUID, dict[str, Any]]] = []

    await call_search(_echo_server(seen), _USER, _args(["deload"]))

    assert seen[0][1]["sport"] is None
