"""`ai-trainer ingest-worker` prints one line per source, never its content (#423)."""

import asyncio
import io
import os
import signal
from uuid import uuid4

import pytest

from ai_trainer import cli, cli_knowledge
from ai_trainer.domain.embeddings import Embedded, EmbeddingFailed, EmbeddingFailure
from ai_trainer.domain.knowledge import KnowledgeSource
from tests.ingestion_support import (
    DIMENSIONS,
    MODEL,
    FrozenClock,
    MemoryQueue,
    RecordedSleep,
    ScriptedEmbeddings,
    guide_markdown,
    ingestion,
    uploaded_source,
)
from tests.knowledge_support import MemoryStorage


def _queue(*markers: str) -> tuple[MemoryQueue, MemoryStorage]:
    storage = MemoryStorage()
    sources = []
    for marker in markers:
        new = uploaded_source(admin_id=uuid4(), marker=marker)
        assert new.storage_key is not None
        storage.objects[new.storage_key] = guide_markdown(marker)
        sources.append(KnowledgeSource(**new.model_dump()))
    return MemoryQueue(*sources), storage


def test_the_command_parses_with_and_without_once() -> None:
    assert cli.parse_args([cli_knowledge.INGEST_WORKER]).once is False
    assert cli.parse_args([cli_knowledge.INGEST_WORKER, "--once"]).once is True


async def test_once_prints_a_line_per_source_and_a_summary() -> None:
    queue, storage = _queue("cli-ok", "cli-fail")
    ok, failing = list(queue.sources)
    # One call per source at this batch size: the first embeds, the second fails.
    three = Embedded(vectors=tuple((0.1,) * DIMENSIONS for _ in range(3)), model=MODEL)
    service = ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(three, EmbeddingFailed(reason=EmbeddingFailure.ERROR)),
        clock=FrozenClock(),
        batch_size=8,
    )
    output = io.StringIO()

    code = await cli_knowledge.ingest_worker(service, once=True, poll_seconds=1.0, output=output)

    assert code == 0
    assert output.getvalue().splitlines() == [
        f"ingest-worker: source={ok} result=ingested chunks=3",
        f"ingest-worker: source={failing} result=failed reason=embedding_failed",
        "ingest-worker: result=ok sources=2",
    ]


async def test_a_gone_source_is_reported_as_gone() -> None:
    queue, storage = _queue("cli-gone")
    (source_id,) = list(queue.sources)

    async def _delete() -> None:
        queue.sources.pop(source_id, None)

    output = io.StringIO()
    service = ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(during=_delete),
        clock=FrozenClock(),
    )

    await cli_knowledge.ingest_worker(service, once=True, poll_seconds=1.0, output=output)

    assert output.getvalue().splitlines()[0] == f"ingest-worker: source={source_id} result=gone"


async def test_without_once_it_keeps_polling() -> None:
    queue, storage = _queue("cli-loop")
    sleep = RecordedSleep()
    output = io.StringIO()
    service = ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        sleep=sleep,
    )

    task = asyncio.create_task(
        cli_knowledge.ingest_worker(service, once=False, poll_seconds=3.0, output=output)
    )
    while not sleep.waits:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert output.getvalue().splitlines() == [
        f"ingest-worker: source={next(iter(queue.sources))} result=ingested chunks=3"
    ]
    assert sleep.waits[0] == 3.0


async def test_a_stop_signal_ends_the_polling_worker_cleanly() -> None:
    """`docker stop` sends SIGTERM to the container's PID 1, which the worker is: without a
    handler the kernel ignores it, and Docker SIGKILLs after its grace period (#462 review). With
    one, the worker cancels its loop, hands back its source and exits 0. SIGUSR1 stands in for
    SIGTERM here, so a failure can't stop the test process."""
    queue, storage = _queue("cli-signal")
    sleep = RecordedSleep()
    output = io.StringIO()
    service = ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        sleep=sleep,
    )

    task = asyncio.create_task(
        cli_knowledge.ingest_worker(
            service, once=False, poll_seconds=3.0, output=output, stop_signals=(signal.SIGUSR1,)
        )
    )
    while not sleep.waits:
        await asyncio.sleep(0)
    os.kill(os.getpid(), signal.SIGUSR1)

    assert await task == 0
    assert output.getvalue().splitlines()[-1] == "ingest-worker: result=stopped"
    # The handler is removed again, so the next SIGUSR1 isn't swallowed by a finished worker.
    assert signal.getsignal(signal.SIGUSR1) in (signal.SIG_DFL, signal.default_int_handler)
