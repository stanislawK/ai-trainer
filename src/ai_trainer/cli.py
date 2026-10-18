"""`ai-trainer` — operational commands for the app (ADR-0021, ADR-0018).

    uv run ai-trainer purge-conversations
    uv run ai-trainer llm-debug-report [--since <ISO time>]

`purge-conversations` hard-deletes every conversation last active more than
`CONVERSATION_RETENTION_DAYS` ago, across users, with its messages by cascade. It prints the
number deleted and nothing else, so no chat content reaches a log (PRD-0004 B26). Production
runs it daily from the host scheduler (ADR-0023).

`llm-debug-report` summarizes `LLM_DEBUG_LOG_DIR` per template and model (#179) and exits 1 when
there is nothing to summarize. It never opens the database. A `--since` without a timezone is
UTC, like every stored time (ADR-0014).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TextIO

from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.application.ports.chat import ConversationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.retention import purge_expired_conversations
from ai_trainer.llm.debug_report import report
from ai_trainer.settings import Settings

PURGE_CONVERSATIONS = "purge-conversations"
LLM_DEBUG_REPORT = "llm-debug-report"


def _aware_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO time: {value!r}") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ai-trainer", description="ai-trainer operations.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        PURGE_CONVERSATIONS,
        help="Delete conversations past CONVERSATION_RETENTION_DAYS and print how many.",
    )
    debug_report = commands.add_parser(
        LLM_DEBUG_REPORT,
        help="Summarize the LLM_DEBUG_LOG lines per template and model.",
    )
    debug_report.add_argument(
        "--since",
        type=_aware_time,
        help="Only lines at or after this ISO time (UTC when it has no timezone).",
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


async def run(argv: Sequence[str], *, output: TextIO = sys.stdout) -> int:
    """Runs one command and returns its exit code."""
    args = parse_args(argv)
    settings = Settings()
    if args.command == LLM_DEBUG_REPORT:
        return report(settings.llm_debug_log_dir, since=args.since, output=output)
    engine = build_engine(str(settings.database_url))
    try:
        clock = UtcClock()
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
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover -- only runs via direct script execution
