"""`ai-trainer` — operational commands for the app (ADR-0021).

    uv run ai-trainer purge-conversations

`purge-conversations` hard-deletes every conversation last active more than
`CONVERSATION_RETENTION_DAYS` ago, across users, with its messages by cascade. It prints the
number deleted and nothing else, so no chat content reaches a log (PRD-0004 B26). Production
runs it daily from the host scheduler (ADR-0023).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import TextIO

from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.application.ports.chat import ConversationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.retention import purge_expired_conversations
from ai_trainer.settings import Settings

PURGE_CONVERSATIONS = "purge-conversations"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ai-trainer", description="ai-trainer operations.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        PURGE_CONVERSATIONS,
        help="Delete conversations past CONVERSATION_RETENTION_DAYS and print how many.",
    )
    return parser.parse_args(argv)


async def purge_conversations(
    *,
    clock: ClockPort,
    conversations: ConversationRepositoryPort,
    retention_days: int,
    output: TextIO,
) -> None:
    deleted = await purge_expired_conversations(
        clock=clock, conversations=conversations, retention_days=retention_days
    )
    output.write(f"{deleted}\n")


async def run(argv: Sequence[str], *, output: TextIO = sys.stdout) -> None:
    args = parse_args(argv)
    settings = Settings()
    engine = build_engine(str(settings.database_url))
    try:
        clock = UtcClock()
        if args.command == PURGE_CONVERSATIONS:
            await purge_conversations(
                clock=clock,
                conversations=SqlAlchemyConversationRepository(
                    build_session_factory(engine),
                    clock=clock,
                    retention_days=settings.conversation_retention_days,
                ),
                retention_days=settings.conversation_retention_days,
                output=output,
            )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    asyncio.run(run(sys.argv[1:] if argv is None else argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover -- only runs via direct script execution
