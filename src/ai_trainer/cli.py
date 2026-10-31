"""`ai-trainer` — operational commands for the app (ADR-0021, ADR-0018).

    uv run ai-trainer purge-conversations
    uv run ai-trainer llm-debug-report [--since <ISO time>]
    uv run ai-trainer import-exercises
    uv run ai-trainer backfill-gym-sessions
    uv run ai-trainer feedback-report [--since <ISO time>]

`purge-conversations` hard-deletes every conversation last active more than
`CONVERSATION_RETENTION_DAYS` ago, across users, with its messages by cascade. It prints the
one outcome line, `purge: result=ok deleted=<n>`, and nothing else, so no chat content reaches a
log (PRD-0004 B26). Production runs it daily from the host scheduler (ADR-0023); the unit's
`ExecStopPost=` prints `purge: result=failed` when the run fails, a stopped container included,
and Alloy ships both lines to Grafana Cloud (ADR-0030, #274).

`llm-debug-report` summarizes `LLM_DEBUG_LOG_DIR` per template and model (#179) and exits 1 when
there is nothing to summarize. It never opens the database. A `--since` without a timezone is
UTC, like every stored time (ADR-0014).

`import-exercises` upserts the exercise catalogue from the dataset commit pinned in `Settings` and
`EXERCISE_CATALOGUE_DIR` (ADR-0031). Running it again changes nothing, and it never touches an
athlete's custom exercises or aliases. It prints one line, `import-exercises: result=ok ...` or
`import-exercises: result=failed reason=<download|catalogue> ...`, the second with exit code 1 and
no row changed.

`backfill-gym-sessions` writes the entries and sets of gym sessions logged before the typed tables
from their v1 payload, with `exercise_id` and `source_id` NULL (ADR-0032, #377). It only inserts,
so every payload stays as stored, and a second run writes nothing. It prints one
`backfill-gym-sessions: skipped session=<id> reason=<reason>` line for each session that won't
read or write, never its content, then `backfill-gym-sessions: result=ok ...` (or
`result=skipped` with exit code 1 when any was skipped); the others still run.

`feedback-report` prints the content-free feedback events (ADR-0034, #394) as one line per kind,
template and field with its count, `<kind> <template|-> <field|-> <count>`, largest first, or
`no feedback events` when there are none. It reads every user's rows without who made them, and
a `--since` without a timezone is UTC. It exits 0 either way.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TextIO

import httpx2

from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.exercises_dataset import PinnedDatasetCatalogueSource
from ai_trainer.adapters.feedback_repository import SqlAlchemyFeedbackEventsRepository
from ai_trainer.adapters.gym_backfill import SqlAlchemyGymBackfillStore
from ai_trainer.application.exercise_catalogue import import_catalogue
from ai_trainer.application.feedback import count_signals
from ai_trainer.application.gym_backfill import backfill_gym_sessions as run_gym_backfill
from ai_trainer.application.ports.chat import ConversationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.exercises import (
    ExerciseCatalogueSourcePort,
    ExerciseRepositoryPort,
)
from ai_trainer.application.ports.feedback import FeedbackEventsPort
from ai_trainer.application.ports.gym_backfill import GymBackfillPort
from ai_trainer.application.retention import purge_expired_conversations
from ai_trainer.domain.exercises import ExerciseCatalogueError, ExerciseDatasetError
from ai_trainer.llm.debug_report import report
from ai_trainer.settings import Settings

PURGE_CONVERSATIONS = "purge-conversations"
LLM_DEBUG_REPORT = "llm-debug-report"
IMPORT_EXERCISES = "import-exercises"
BACKFILL_GYM_SESSIONS = "backfill-gym-sessions"
FEEDBACK_REPORT = "feedback-report"


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
        help="Delete conversations past CONVERSATION_RETENTION_DAYS and print the outcome line.",
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
    commands.add_parser(
        IMPORT_EXERCISES,
        help="Upsert the exercise catalogue from the pinned dataset commit and print the outcome.",
    )
    commands.add_parser(
        BACKFILL_GYM_SESSIONS,
        help="Write gym sessions logged before the typed tables into them; payloads stay put.",
    )
    feedback = commands.add_parser(
        FEEDBACK_REPORT,
        help="Count the content-free feedback events per kind, template and field.",
    )
    feedback.add_argument(
        "--since",
        type=_aware_time,
        help="Only events at or after this ISO time (UTC when it has no timezone).",
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
    output.write(f"purge: result=ok deleted={deleted}\n")


async def import_exercises(
    *,
    source: ExerciseCatalogueSourcePort,
    exercises: ExerciseRepositoryPort,
    output: TextIO,
) -> int:
    try:
        done = await import_catalogue(source=source, exercises=exercises)
    except ExerciseDatasetError as exc:
        return _import_failed("download", exc, output)
    except ExerciseCatalogueError as exc:
        return _import_failed("catalogue", exc, output)
    output.write(
        f"import-exercises: result=ok exercises={done.total} inserted={done.write.inserted} "
        f"updated={done.write.updated} unchanged={done.write.unchanged} "
        f"texts_written={done.write.texts_written} skipped={done.skipped}\n"
    )
    return 0


async def backfill_gym_sessions(*, store: GymBackfillPort, output: TextIO) -> int:
    report = await run_gym_backfill(store=store)
    for skip in report.skipped:
        output.write(
            f"{BACKFILL_GYM_SESSIONS}: skipped session={skip.session_id} reason={skip.reason}\n"
        )
    result = "skipped" if report.skipped else "ok"
    output.write(
        f"{BACKFILL_GYM_SESSIONS}: result={result} sessions={report.sessions} "
        f"entries={report.entries} sets={report.sets} skipped={len(report.skipped)}\n"
    )
    return 1 if report.skipped else 0


async def feedback_report(
    *, events: FeedbackEventsPort, since: datetime | None, output: TextIO
) -> int:
    counts = count_signals(await events.signals(since=since))
    if not counts:
        output.write("no feedback events\n")
        return 0
    for row in counts:
        output.write(f"{row.kind.value} {row.template_id or '-'} {row.field or '-'} {row.count}\n")
    return 0


def _import_failed(reason: str, error: Exception, output: TextIO) -> int:
    detail = " ".join(str(error).split())
    output.write(f"import-exercises: result=failed reason={reason} detail={detail}\n")
    return 1


async def run(
    argv: Sequence[str],
    *,
    output: TextIO = sys.stdout,
    dataset_transport: httpx2.AsyncBaseTransport | None = None,
) -> int:
    """Runs one command and returns its exit code. `dataset_transport` is for tests."""
    args = parse_args(argv)
    settings = Settings()
    if args.command == LLM_DEBUG_REPORT:
        return report(settings.llm_debug_log_dir, since=args.since, output=output)
    engine = build_engine(str(settings.database_url))
    try:
        if args.command == IMPORT_EXERCISES:
            return await import_exercises(
                source=PinnedDatasetCatalogueSource(
                    commit=settings.exercise_dataset_commit,
                    fetched_on=settings.exercise_dataset_fetched_on,
                    directory=settings.exercise_catalogue_dir,
                    transport=dataset_transport,
                ),
                exercises=SqlAlchemyExerciseRepository(build_session_factory(engine)),
                output=output,
            )
        if args.command == BACKFILL_GYM_SESSIONS:
            return await backfill_gym_sessions(
                store=SqlAlchemyGymBackfillStore(build_session_factory(engine)), output=output
            )
        if args.command == FEEDBACK_REPORT:
            return await feedback_report(
                events=SqlAlchemyFeedbackEventsRepository(build_session_factory(engine)),
                since=args.since,
                output=output,
            )
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
