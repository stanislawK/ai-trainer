"""`ai-trainer` — operational commands for the app (ADR-0021, ADR-0018).

    uv run ai-trainer purge-conversations
    uv run ai-trainer llm-debug-report [--since <ISO time>]
    uv run ai-trainer import-exercises
    uv run ai-trainer backfill-gym-sessions
    uv run ai-trainer feedback-report [--since <ISO time>]
    uv run ai-trainer storage-check
    uv run ai-trainer ingest-worker [--once]

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
athlete's custom exercises or aliases. It then queues each of the catalogue's two knowledge
sources, English and Polish, that is missing, failed or has a changed text, for `ingest-worker`,
which embeds only the texts that changed (ADR-0036's #427 amendment); the line's
`catalogue_queued=<n>` says how many. It prints one
line, `import-exercises: result=ok ...` or `import-exercises: result=failed
reason=<download|catalogue> ...`, the second with exit code 1 and no row changed.

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

`storage-check` proves the configured `uploads` bucket works (ADR-0033, #420): it puts a probe
object under `storage-check/`, reads it back, opens a short presigned link until it expires, and
deletes the object, in about five seconds. It never opens the database. It prints one line,
`storage-check: result=ok bucket=<name> steps=<steps>` or
`storage-check: result=failed step=<step> reason=<reason> probe_left=<true|false>`, the second with
exit code 1, or `storage-check: result=failed reason=not_configured`. It never prints a link or a
service's own message, which can carry a key.

`ingest-worker` ingests uploaded knowledge sources outside the web process (ADR-0036, #423): it
claims each `uploaded` source, reads, chunks and embeds it, and stores its chunks. It prints one
line per source, `ingest-worker: source=<id> result=ingested chunks=<n>`,
`result=failed reason=<reason>` or `result=gone` (deleted while it was read), never its content.
With `--once` it ingests what is waiting, prints `ingest-worker: result=ok sources=<n>` and exits
0; without, it polls every `KNOWLEDGE_INGEST_POLL_SECONDS` until stopped. With no storage
settings it prints `ingest-worker: result=failed reason=not_configured` and exits 1.
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import TextIO
from uuid import uuid4

import httpx2

from ai_trainer.adapters.catalogue_knowledge_repository import SqlAlchemyCatalogueSources
from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.exercises_dataset import PinnedDatasetCatalogueSource
from ai_trainer.adapters.feedback_repository import SqlAlchemyFeedbackEventsRepository
from ai_trainer.adapters.gym_backfill import SqlAlchemyGymBackfillStore
from ai_trainer.adapters.object_storage import UrllibPresignedLinks
from ai_trainer.application.exercise_catalogue import CatalogueQueueing, import_catalogue
from ai_trainer.application.feedback import count_signals
from ai_trainer.application.gym_backfill import backfill_gym_sessions as run_gym_backfill
from ai_trainer.application.knowledge_ingestion import KnowledgeIngestion
from ai_trainer.application.ports.chat import ConversationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.exercises import (
    ExerciseCatalogueSourcePort,
    ExerciseRepositoryPort,
)
from ai_trainer.application.ports.feedback import FeedbackEventsPort
from ai_trainer.application.ports.gym_backfill import GymBackfillPort
from ai_trainer.application.retention import purge_expired_conversations
from ai_trainer.application.storage_check import STEPS, check_storage
from ai_trainer.domain.exercises import ExerciseCatalogueError, ExerciseDatasetError
from ai_trainer.domain.knowledge_ingestion import IngestReport
from ai_trainer.llm.debug_report import report
from ai_trainer.main import build_knowledge_ingestion, build_uploads_storage
from ai_trainer.settings import Settings

PURGE_CONVERSATIONS = "purge-conversations"
LLM_DEBUG_REPORT = "llm-debug-report"
IMPORT_EXERCISES = "import-exercises"
BACKFILL_GYM_SESSIONS = "backfill-gym-sessions"
FEEDBACK_REPORT = "feedback-report"
STORAGE_CHECK = "storage-check"
INGEST_WORKER = "ingest-worker"


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
    commands.add_parser(
        STORAGE_CHECK,
        help="Put, read back and delete a probe object in the uploads bucket; try a short link.",
    )
    worker = commands.add_parser(
        INGEST_WORKER, help="Ingest uploaded knowledge sources, outside the web process."
    )
    worker.add_argument(
        "--once", action="store_true", help="Ingest what is waiting now, then exit."
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
    knowledge: CatalogueQueueing,
    output: TextIO,
) -> int:
    try:
        done = await import_catalogue(source=source, exercises=exercises, knowledge=knowledge)
    except ExerciseDatasetError as exc:
        return _import_failed("download", exc, output)
    except ExerciseCatalogueError as exc:
        return _import_failed("catalogue", exc, output)
    output.write(
        f"import-exercises: result=ok exercises={done.total} inserted={done.write.inserted} "
        f"updated={done.write.updated} unchanged={done.write.unchanged} "
        f"texts_written={done.write.texts_written} skipped={done.skipped} "
        f"catalogue_queued={done.catalogue_queued}\n"
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


async def storage_check(
    settings: Settings, *, sleep: Callable[[float], Awaitable[None]], output: TextIO
) -> int:
    storage = build_uploads_storage(settings)
    if storage is None:
        output.write(f"{STORAGE_CHECK}: result=failed reason=not_configured\n")
        return 1
    failure = await check_storage(
        storage=storage,
        links=UrllibPresignedLinks(timeout_seconds=settings.object_storage_timeout_seconds),
        sleep=sleep,
        probe_key=f"storage-check/{uuid4()}",
    )
    if failure is not None:
        probe_left = str(failure.probe_left).lower()
        output.write(
            f"{STORAGE_CHECK}: result=failed step={failure.step} reason={failure.reason} "
            f"probe_left={probe_left}\n"
        )
        return 1
    bucket = settings.object_storage_uploads_bucket
    output.write(f"{STORAGE_CHECK}: result=ok bucket={bucket} steps={','.join(STEPS)}\n")
    return 0


def _print_report(report: IngestReport, output: TextIO) -> None:
    line = f"{INGEST_WORKER}: source={report.source_id} result={report.result}"
    if report.result == "ingested":
        line += f" chunks={report.chunks}"
    elif report.reason is not None:
        line += f" reason={report.reason.value}"
    output.write(line + "\n")
    output.flush()


async def ingest_worker(
    service: KnowledgeIngestion, *, once: bool, poll_seconds: float, output: TextIO
) -> int:
    if once:
        reports = await service.drain()
        for report in reports:
            _print_report(report, output)
        output.write(f"{INGEST_WORKER}: result=ok sources={len(reports)}\n")
        return 0

    async def _report(report: IngestReport) -> None:
        _print_report(report, output)

    await service.run(poll_seconds=poll_seconds, on_report=_report)
    return 0  # pragma: no cover -- `run` returns only by cancellation


def _import_failed(reason: str, error: Exception, output: TextIO) -> int:
    detail = " ".join(str(error).split())
    output.write(f"import-exercises: result=failed reason={reason} detail={detail}\n")
    return 1


async def run(
    argv: Sequence[str],
    *,
    output: TextIO = sys.stdout,
    dataset_transport: httpx2.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    embedding_transport: httpx2.AsyncBaseTransport | None = None,
    pmc_transport: httpx2.AsyncBaseTransport | None = None,
) -> int:
    """Runs one command and returns its exit code. `dataset_transport`, `sleep`,
    `embedding_transport` and `pmc_transport` are for tests."""
    args = parse_args(argv)
    settings = Settings()
    if args.command == LLM_DEBUG_REPORT:
        return report(settings.llm_debug_log_dir, since=args.since, output=output)
    if args.command == STORAGE_CHECK:
        return await storage_check(settings, sleep=sleep, output=output)
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
                knowledge=CatalogueQueueing(
                    sources=SqlAlchemyCatalogueSources(build_session_factory(engine)),
                    admin_emails=settings.admin_emails,
                    clock=UtcClock(),
                ),
                output=output,
            )
        if args.command == BACKFILL_GYM_SESSIONS:
            return await backfill_gym_sessions(
                store=SqlAlchemyGymBackfillStore(build_session_factory(engine)), output=output
            )
        if args.command == INGEST_WORKER:
            storage = build_uploads_storage(settings)
            if storage is None:
                output.write(f"{INGEST_WORKER}: result=failed reason=not_configured\n")
                return 1
            service = build_knowledge_ingestion(
                settings,
                storage,
                build_session_factory(engine),
                sleep=sleep,
                embedding_transport=embedding_transport,
                pmc_transport=pmc_transport,
            )
            return await ingest_worker(
                service,
                once=args.once,
                poll_seconds=settings.knowledge_ingest_poll_seconds,
                output=output,
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
