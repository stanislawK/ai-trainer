"""The knowledge commands of `ai-trainer` (ADR-0036): `ingest-worker`, which the `ingest`
container runs, `rebuild-knowledge-index` (#462), and `storage-check` on the uploads bucket
(ADR-0033). `cli` parses and wires them; their behaviour is described in its docstring."""

import argparse
import asyncio
import signal
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO
from uuid import uuid4

from ai_trainer.adapters.object_storage import UrllibPresignedLinks
from ai_trainer.application.knowledge_ingestion import KnowledgeIngestion
from ai_trainer.application.ports.knowledge import KnowledgeIndexPort
from ai_trainer.application.storage_check import STEPS, check_storage
from ai_trainer.domain.knowledge_ingestion import IngestReport
from ai_trainer.main import build_uploads_storage
from ai_trainer.settings import Settings

INGEST_WORKER = "ingest-worker"
REBUILD_KNOWLEDGE_INDEX = "rebuild-knowledge-index"
STORAGE_CHECK = "storage-check"


def build_memory_mb(value: str) -> int:
    """The bounds of `KNOWLEDGE_INDEX_BUILD_MEMORY_MB`: at least the server's 64 MB default, at
    most the db container's 256 MiB `shm_size` (#462)."""
    memory_mb = int(value)
    if not 64 <= memory_mb <= 256:
        raise argparse.ArgumentTypeError("must be 64 to 256 (the db container's shm_size)")
    return memory_mb


def _print_report(report: IngestReport, output: TextIO) -> None:
    line = f"{INGEST_WORKER}: source={report.source_id} result={report.result}"
    if report.result == "ingested":
        line += f" chunks={report.chunks}"
    elif report.reason is not None:
        line += f" reason={report.reason.value}"
    output.write(line + "\n")
    output.flush()


async def ingest_worker(
    service: KnowledgeIngestion,
    *,
    once: bool,
    poll_seconds: float,
    output: TextIO,
    stop_signals: Sequence[signal.Signals] = (signal.SIGTERM, signal.SIGINT),
) -> int:
    if once:
        reports = await service.drain()
        for report in reports:
            _print_report(report, output)
        output.write(f"{INGEST_WORKER}: result=ok sources={len(reports)}\n")
        return 0

    async def _report(report: IngestReport) -> None:
        _print_report(report, output)

    # The `ingest` container's PID 1 is this process, and the kernel ignores a signal PID 1 has
    # no handler for, so `docker stop` would wait out its grace period and SIGKILL (#462). A stop
    # signal cancels the loop instead, which hands the source being read back as `uploaded`.
    loop = asyncio.get_running_loop()
    running = asyncio.current_task()
    assert running is not None
    stopping = asyncio.Event()

    def _stop() -> None:
        stopping.set()
        running.cancel()

    for stop_signal in stop_signals:
        loop.add_signal_handler(stop_signal, _stop)
    try:
        await service.run(poll_seconds=poll_seconds, on_report=_report)
    except asyncio.CancelledError:
        if not stopping.is_set():
            raise
        output.write(f"{INGEST_WORKER}: result=stopped\n")
        return 0
    finally:
        for stop_signal in stop_signals:
            loop.remove_signal_handler(stop_signal)
    return 0  # pragma: no cover -- `run` returns only by cancellation


async def idle_unconfigured(output: TextIO) -> int:
    """The `ingest` container with no bucket configured (`make up`, CI, the smoke stack, #462):
    one line, then wait until stopped, so a restart policy never spins on it."""
    output.write(f"{INGEST_WORKER}: result=idle reason=not_configured\n")
    output.flush()
    await asyncio.Event().wait()
    return 0  # pragma: no cover -- the wait returns only by cancellation


async def rebuild_knowledge_index(
    index: KnowledgeIndexPort, *, memory_mb: int, output: TextIO
) -> int:
    try:
        used = await index.rebuild(memory_mb=memory_mb)
    except Exception as exc:
        # The class only: a driver's message can name the server or the statement.
        output.write(f"{REBUILD_KNOWLEDGE_INDEX}: result=failed reason={type(exc).__name__}\n")
        return 1
    output.write(
        f"{REBUILD_KNOWLEDGE_INDEX}: result=ok index={index.index} maintenance_work_mem={used}\n"
    )
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
