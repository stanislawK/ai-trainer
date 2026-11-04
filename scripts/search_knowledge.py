"""Calls `search_knowledge` on the app's MCP server in process, as the seeded dev user, and
prints the tool's result as JSON (ADR-0010, ADR-0036 #424 amendment).

Run with `make search-knowledge q="how often should I deload?" [sport=gym] [limit=5]`, which is
`uv run python -m scripts.search_knowledge "<question>" [--sport gym] [--limit 5]`, against the
database `DATABASE_URL` names, such as `make start`'s. It makes one real
embedding call through OpenRouter (`OPENROUTER_API_KEY`, about $0.00001) and writes its
`llm_calls` row for the dev user, as a specialist's search would. The MCP Inspector can't stand
in: the server has no stdio or HTTP entry, and the Inspector sends no user in the request meta.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any, TextIO
from uuid import UUID

from fastmcp import Client, FastMCP

from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.main import build_activity_stores, build_app_mcp_server, query_http_client
from ai_trainer.mcp.identity import USER_ID_META_KEY
from ai_trainer.mcp.server import SEARCH_KNOWLEDGE_TOOL
from ai_trainer.settings import Settings
from scripts.dev_session import DEV_SESSION_EMAIL, DEV_SESSION_SUB, get_or_create_active_user


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Call search_knowledge as the dev user.")
    parser.add_argument("query")
    parser.add_argument("--sport")
    parser.add_argument("--limit", type=int, default=5)
    return parser.parse_args(argv)


async def call_search(
    server: FastMCP, user_id: UUID, args: argparse.Namespace
) -> dict[str, Any] | None:
    arguments: dict[str, Any] = {"query": args.query, "limit": args.limit}
    if args.sport is not None:
        arguments["sport"] = args.sport
    async with Client(server) as client:
        # A masked server error comes back as a result to print, not a traceback.
        result = await client.call_tool(
            SEARCH_KNOWLEDGE_TOOL,
            arguments,
            meta={USER_ID_META_KEY: str(user_id)},
            raise_on_error=False,
        )
    if result.is_error:
        return {"success": False, "error": "the tool failed; see the app's log"}
    content: dict[str, Any] | None = result.structured_content
    return content


async def run(  # pragma: no cover -- real database and OpenRouter; the PR's Acceptance runs it
    args: argparse.Namespace, out: TextIO
) -> int:
    settings = Settings()
    engine = build_engine(str(settings.database_url))
    http_client = query_http_client(settings)
    try:
        factory = build_session_factory(engine)
        sports = default_sport_registry()
        user = await get_or_create_active_user(
            SqlAlchemyUsersRepository(factory), sub=DEV_SESSION_SUB, email=DEV_SESSION_EMAIL
        )
        server = build_app_mcp_server(
            settings,
            factory,
            sports,
            training_sessions=SqlAlchemyTrainingSessionRepository(
                factory, stores=build_activity_stores(sports, settings.max_clarification_options)
            ),
            calls_repository=SqlAlchemyLlmCallsRepository(factory),
            http_client=http_client,
        )
        result = await call_search(server, user.id, args)
    finally:
        await http_client.aclose()
        await engine.dispose()
    json.dump(result, out, indent=2, ensure_ascii=False)
    out.write("\n")
    return 0 if result is not None and result.get("success") else 1


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover -- the CLI entry point
    return asyncio.run(run(parse_args(sys.argv[1:] if argv is None else argv), sys.stdout))


if __name__ == "__main__":  # pragma: no cover -- run as a script
    raise SystemExit(main())
