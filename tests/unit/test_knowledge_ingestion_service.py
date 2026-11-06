"""The ingestion use case on fakes (ADR-0036, #423): batches with backoff on 429, every failure
a closed reason, a lost claim or a deleted source writing nothing, the heartbeat renewing the
lease. The ACs on the real database are in `tests/integration/test_knowledge_ingestion.py`."""

import asyncio
import hashlib
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.domain.embeddings import (
    Embedded,
    EmbeddingFailed,
    EmbeddingFailure,
    RateLimited,
)
from ai_trainer.domain.knowledge import KnowledgeFileType, KnowledgeSource, SourceStatus
from ai_trainer.domain.knowledge_ingestion import IngestFailure, IngestReport
from ai_trainer.domain.pmc import PmcFetchProblem, PmcNotFound, PmcUnavailable
from ai_trainer.knowledge import readers as readers_module
from tests.ingestion_support import (
    DIMENSIONS,
    MODEL,
    FakePmcFiles,
    FrozenClock,
    MemoryQueue,
    RecordedSleep,
    ScriptedEmbeddings,
    guide_markdown,
    ingestion,
    pmc_source,
    uploaded_source,
)
from tests.knowledge_support import MemoryStorage
from tests.pdf_support import blank_pdf, encrypted_pdf, text_pdf

_ADMIN = uuid4()


def _stored(
    marker: str,
    *,
    data: bytes | None = None,
    file_type: KnowledgeFileType = KnowledgeFileType.MARKDOWN,
    admin_id: UUID | None = _ADMIN,
) -> tuple[KnowledgeSource, MemoryStorage]:
    new = uploaded_source(admin_id=admin_id, marker=marker, file_type=file_type)
    storage = MemoryStorage()
    assert new.storage_key is not None
    storage.objects[new.storage_key] = guide_markdown(marker) if data is None else data
    return KnowledgeSource(**new.model_dump()), storage


async def test_chunks_are_embedded_in_batches_as_title_and_heading_path_then_text() -> None:
    source, storage = _stored("batches")
    queue = MemoryQueue(source)
    embeddings = ScriptedEmbeddings()

    report = await ingestion(
        repository=queue, storage=storage, embeddings=embeddings, clock=FrozenClock()
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="ingested", chunks=3)
    assert [len(texts) for texts, _ in embeddings.calls] == [2, 1]
    assert {admin for _, admin in embeddings.calls} == {_ADMIN}
    first = embeddings.calls[0][0][0]
    assert first.startswith(f"{source.title} — Deload guide > Why\n\nFatigue masks")
    assert queue.models[source.id] == MODEL
    assert [c.chunk.ordinal for c in queue.chunks[source.id]] == [0, 1, 2]
    assert all(len(c.embedding) == DIMENSIONS for c in queue.chunks[source.id])


async def test_nothing_to_claim_reports_nothing() -> None:
    queue = MemoryQueue()

    report = await ingestion(
        repository=queue,
        storage=MemoryStorage(),
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
    ).ingest_next()

    assert report is None


async def test_drain_ingests_every_queued_source_then_stops() -> None:
    first, storage = _stored("drain-1")
    second = KnowledgeSource(**uploaded_source(admin_id=_ADMIN, marker="drain-2").model_dump())
    assert second.storage_key is not None
    storage.objects[second.storage_key] = guide_markdown("drain-2")
    queue = MemoryQueue(first, second)

    reports = await ingestion(
        repository=queue, storage=storage, embeddings=ScriptedEmbeddings(), clock=FrozenClock()
    ).drain()

    assert [(r.source_id, r.result) for r in reports] == [
        (first.id, "ingested"),
        (second.id, "ingested"),
    ]


async def test_a_rate_limited_batch_backs_off_then_succeeds() -> None:
    source, storage = _stored("backoff")
    sleep = RecordedSleep()
    embeddings = ScriptedEmbeddings(RateLimited(), RateLimited(retry_after_seconds=7.0))

    report = await ingestion(
        repository=MemoryQueue(source),
        storage=storage,
        embeddings=embeddings,
        clock=FrozenClock(),
        sleep=sleep,
        max_attempts=3,
    ).ingest_next()

    assert report is not None

    assert report.result == "ingested"
    assert sleep.waits == [1.0, 7.0]
    # The rate-limited batch is sent three times, then the second batch once.
    assert len(embeddings.calls) == 4


async def test_backoff_doubles_up_to_its_cap() -> None:
    source, storage = _stored("cap")
    sleep = RecordedSleep()

    await ingestion(
        repository=MemoryQueue(source),
        storage=storage,
        embeddings=ScriptedEmbeddings(*[RateLimited()] * 7),
        clock=FrozenClock(),
        sleep=sleep,
        max_attempts=8,
    ).ingest_next()

    assert sleep.waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


async def test_rate_limits_past_the_attempts_fail_the_source() -> None:
    source, storage = _stored("too-many")
    queue = MemoryQueue(source)

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(*[RateLimited()] * 3),
        clock=FrozenClock(),
        max_attempts=3,
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.RATE_LIMITED
    )
    assert queue.sources[source.id].error == "rate_limited"
    assert source.id not in queue.chunks


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (EmbeddingFailure.ERROR, IngestFailure.EMBEDDING_FAILED),
        (EmbeddingFailure.TIMEOUT, IngestFailure.EMBEDDING_TIMEOUT),
        (EmbeddingFailure.WRONG_SHAPE, IngestFailure.WRONG_SHAPE),
    ],
    ids=["error", "timeout", "wrong_shape"],
)
async def test_an_embedding_failure_fails_the_source_without_retrying(
    failure: EmbeddingFailure, reason: IngestFailure
) -> None:
    source, storage = _stored(f"fail-{failure}")
    queue = MemoryQueue(source)
    embeddings = ScriptedEmbeddings(EmbeddingFailed(reason=failure))

    report = await ingestion(
        repository=queue, storage=storage, embeddings=embeddings, clock=FrozenClock()
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="failed", reason=reason)
    assert len(embeddings.calls) == 1


async def test_a_vector_of_the_wrong_length_fails_the_source() -> None:
    source, storage = _stored("short-vector")
    embeddings = ScriptedEmbeddings(Embedded(vectors=((0.1,) * 3, (0.2,) * 3), model=MODEL))

    report = await ingestion(
        repository=MemoryQueue(source), storage=storage, embeddings=embeddings, clock=FrozenClock()
    ).ingest_next()

    assert report is not None

    assert report.reason is IngestFailure.WRONG_SHAPE


@pytest.mark.parametrize(
    ("data", "file_type", "reason"),
    [
        (b"<article/>", KnowledgeFileType.JATS, IngestFailure.NO_TEXT),
        (b"PK\x03\x04", KnowledgeFileType.EPUB, IngestFailure.CORRUPT_FILE),
        (blank_pdf(), KnowledgeFileType.PDF, IngestFailure.NO_TEXT_LAYER),
        (b"%PDF-1.7 broken", KnowledgeFileType.PDF, IngestFailure.CORRUPT_FILE),
        (
            encrypted_pdf(text_pdf([["Secret words."]]), user_password="pw"),
            KnowledgeFileType.PDF,
            IngestFailure.ENCRYPTED_FILE,
        ),
        (b"\xff\xfe", KnowledgeFileType.MARKDOWN, IngestFailure.NOT_UTF8),
        (b"# Only a title\n", KnowledgeFileType.MARKDOWN, IngestFailure.NO_TEXT),
        (b"## References\n\nSmith 2020.\n", KnowledgeFileType.MARKDOWN, IngestFailure.NO_TEXT),
    ],
    ids=[
        "empty_jats",
        "corrupt_epub",
        "scanned_pdf",
        "corrupt_pdf",
        "encrypted_pdf",
        "not_utf8",
        "only_headings",
        "only_references",
    ],
)
async def test_a_file_that_gives_no_chunks_fails_with_its_reason_and_no_call(
    data: bytes, file_type: KnowledgeFileType, reason: IngestFailure
) -> None:
    source, storage = _stored(f"unreadable-{reason}", data=data, file_type=file_type)
    embeddings = ScriptedEmbeddings()

    report = await ingestion(
        repository=MemoryQueue(source), storage=storage, embeddings=embeddings, clock=FrozenClock()
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="failed", reason=reason)
    assert embeddings.calls == []


async def test_a_file_source_with_no_file_type_fails_as_missing() -> None:
    source, storage = _stored("no-type")
    typeless = source.model_copy(update={"file_type": None})
    embeddings = ScriptedEmbeddings()

    report = await ingestion(
        repository=MemoryQueue(typeless),
        storage=storage,
        embeddings=embeddings,
        clock=FrozenClock(),
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.FILE_MISSING
    )
    assert embeddings.calls == []


async def test_a_file_type_with_no_reader_fails_with_no_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every type has a reader now; the registry's gap is simulated by emptying it.
    monkeypatch.setattr(readers_module, "_READERS", {})
    source, storage = _stored("no-reader", data=b"x", file_type=KnowledgeFileType.PDF)
    embeddings = ScriptedEmbeddings()

    report = await ingestion(
        repository=MemoryQueue(source), storage=storage, embeddings=embeddings, clock=FrozenClock()
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.NO_READER
    )
    assert embeddings.calls == []


async def test_a_missing_file_fails_the_source() -> None:
    source, storage = _stored("missing")
    storage.objects.clear()

    report = await ingestion(
        repository=MemoryQueue(source),
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
    ).ingest_next()

    assert report is not None

    assert report.reason is IngestFailure.FILE_MISSING


async def test_a_storage_outage_fails_the_source() -> None:
    source, storage = _stored("outage")

    async def _broken(key: str) -> bytes:
        from ai_trainer.domain.object_storage import ObjectStorageError

        raise ObjectStorageError("get", "unavailable")

    storage.get = _broken  # type: ignore[method-assign]  # a storage that fails on read

    report = await ingestion(
        repository=MemoryQueue(source),
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
    ).ingest_next()

    assert report is not None

    assert report.reason is IngestFailure.STORAGE_FAILED


async def test_a_source_whose_uploader_is_gone_fails_with_no_call() -> None:
    source, storage = _stored("orphan", admin_id=None)
    embeddings = ScriptedEmbeddings()

    report = await ingestion(
        repository=MemoryQueue(source), storage=storage, embeddings=embeddings, clock=FrozenClock()
    ).ingest_next()

    assert report is not None

    assert report.reason is IngestFailure.NO_ACTING_ADMIN
    assert embeddings.calls == []


async def test_a_source_with_no_file_fails_as_missing() -> None:
    source, storage = _stored("no-key")
    source = source.model_copy(update={"storage_key": None})

    report = await ingestion(
        repository=MemoryQueue(source),
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
    ).ingest_next()

    assert report is not None

    assert report.reason is IngestFailure.FILE_MISSING


async def test_a_source_deleted_while_embedding_is_gone_and_writes_nothing() -> None:
    source, storage = _stored("deleted")
    queue = MemoryQueue(source)

    async def _delete() -> None:
        queue.sources.pop(source.id, None)

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(during=_delete),
        clock=FrozenClock(),
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="gone")
    assert queue.chunks == {}


async def test_a_failure_on_a_source_deleted_meanwhile_is_gone() -> None:
    source, storage = _stored("deleted-failing")
    queue = MemoryQueue(source)

    async def _delete() -> None:
        queue.sources.pop(source.id, None)

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(
            EmbeddingFailed(reason=EmbeddingFailure.ERROR), during=_delete
        ),
        clock=FrozenClock(),
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="gone")


async def test_the_heartbeat_renews_the_lease_while_the_source_is_read() -> None:
    source, storage = _stored("heartbeat")
    queue = MemoryQueue(source)

    async def _slow() -> None:
        await asyncio.sleep(0.05)

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(during=_slow),
        clock=FrozenClock(),
        lease=timedelta(milliseconds=30),
    ).ingest_next()

    assert report is not None

    assert report.result == "ingested"
    assert queue.heartbeats >= 2


async def test_a_lost_claim_stops_before_the_next_batch() -> None:
    source, storage = _stored("stolen")
    queue = MemoryQueue(source)
    queue.steal_on_heartbeat = True
    embeddings = ScriptedEmbeddings(during=lambda: asyncio.sleep(0.05))

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=embeddings,
        clock=FrozenClock(),
        lease=timedelta(milliseconds=30),
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="gone")
    assert len(embeddings.calls) == 1
    assert queue.chunks == {}


async def test_a_claim_lost_during_the_last_batch_writes_nothing() -> None:
    source, storage = _stored("stolen-last")
    queue = MemoryQueue(source)
    queue.steal_on_heartbeat = True

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(during=lambda: asyncio.sleep(0.05)),
        clock=FrozenClock(),
        batch_size=8,
        lease=timedelta(milliseconds=30),
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="gone")
    assert queue.chunks == {}


async def test_a_retry_after_is_kept_within_zero_and_the_cap() -> None:
    source, storage = _stored("retry-after-bounds")
    sleep = RecordedSleep()

    await ingestion(
        repository=MemoryQueue(source),
        storage=storage,
        embeddings=ScriptedEmbeddings(
            RateLimited(retry_after_seconds=86400.0), RateLimited(retry_after_seconds=-5.0)
        ),
        clock=FrozenClock(),
        sleep=sleep,
    ).ingest_next()

    assert sleep.waits == [30.0, 0.0]


async def test_an_unexpected_error_fails_the_source_instead_of_crashing_the_worker() -> None:
    source, storage = _stored("unexpected")
    queue = MemoryQueue(source)

    async def _explode() -> None:
        raise RuntimeError("a bug, not a provider failure")

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(during=_explode),
        clock=FrozenClock(),
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.INGEST_ERROR
    )
    assert queue.sources[source.id].error == "ingest_error"


async def test_a_heartbeat_that_errors_keeps_beating_and_never_fails_the_ingest() -> None:
    source, storage = _stored("heartbeat-blip")
    queue = MemoryQueue(source)
    calls = 0
    original = queue.heartbeat

    async def _flaky(source_id: UUID, *, claim_id: UUID, lease_until: datetime) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("database blip")
        return await original(source_id, claim_id=claim_id, lease_until=lease_until)

    queue.heartbeat = _flaky  # type: ignore[method-assign]  # a repository whose first beat fails

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(during=lambda: asyncio.sleep(0.05)),
        clock=FrozenClock(),
        lease=timedelta(milliseconds=30),
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="ingested", chunks=3)
    assert calls >= 2


async def test_run_polls_until_cancelled() -> None:
    source, storage = _stored("run")
    queue = MemoryQueue(source)
    sleep = RecordedSleep()
    service = ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        sleep=sleep,
    )
    seen: list[IngestReport] = []

    async def _report(report: IngestReport) -> None:
        seen.append(report)

    task = asyncio.create_task(service.run(poll_seconds=2.0, on_report=_report))
    while len(sleep.waits) < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [r.source_id for r in seen] == [source.id]
    assert sleep.waits[:2] == [2.0, 2.0]


# --- PMC articles from JATS (#463) ----------------------------------------------------------

_PMCID = "PMC4242"
_JATS = b"""<article><front><article-meta>
<article-id pub-id-type="doi">10.1234/Demo</article-id>
<abstract><p>Short abstract about training volume.</p></abstract></article-meta></front>
<body>
<sec><title>Introduction</title><p>Volume drives hypertrophy in trained lifters over time.</p></sec>
<sec><title>Methods</title><p>We counted hard sets per muscle group each week.</p></sec>
</body><back><ref-list><title>References</title>
<ref><mixed-citation>Ref one.</mixed-citation></ref></ref-list></back>
</article>"""


def _pmc(
    pmcid: str = _PMCID, *, admin_id: UUID | None = _ADMIN
) -> tuple[KnowledgeSource, MemoryStorage]:
    return KnowledgeSource(
        **pmc_source(admin_id=admin_id, pmcid=pmcid).model_dump()
    ), MemoryStorage()


async def test_pmc_source_is_fetched_recorded_and_ingested() -> None:
    source, storage = _pmc()
    queue = MemoryQueue(source)
    files = FakePmcFiles({_PMCID: _JATS})

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        pmc_files=files,
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="ingested", chunks=3)
    assert files.requests == [_PMCID]
    key = f"sources/{source.id}"
    assert storage.objects == {key: _JATS}
    assert storage.content_types[key] == "application/xml"
    recorded = queue.sources[source.id]
    assert recorded.storage_key == key
    assert recorded.file_name == f"{_PMCID}.xml"
    assert recorded.size_bytes == len(_JATS)
    assert recorded.content_hash == hashlib.sha256(_JATS).hexdigest()
    paths = [c.chunk.heading_path for c in queue.chunks[source.id]]
    assert paths == [("Abstract",), ("Introduction",), ("Methods",)]
    assert not any("Ref one" in c.chunk.text for c in queue.chunks[source.id])


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        pytest.param(PmcNotFound(), IngestFailure.PMC_NOT_FOUND, id="not-found"),
        pytest.param(
            PmcUnavailable(PmcFetchProblem.TIMEOUT), IngestFailure.PMC_FETCH_FAILED, id="timeout"
        ),
        pytest.param(
            PmcUnavailable(PmcFetchProblem.MALFORMED), IngestFailure.PMC_FETCH_FAILED, id="bad"
        ),
    ],
)
async def test_pmc_fetch_failures_set_failed_with_a_reason(
    answer: PmcNotFound | PmcUnavailable, reason: IngestFailure
) -> None:
    source, storage = _pmc()
    queue = MemoryQueue(source)
    embeddings = ScriptedEmbeddings()

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=embeddings,
        clock=FrozenClock(),
        pmc_files=FakePmcFiles({_PMCID: answer}),
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="failed", reason=reason)
    assert queue.sources[source.id].status is SourceStatus.FAILED
    assert queue.sources[source.id].error == reason.value
    assert queue.sources[source.id].storage_key is None
    assert storage.objects == {}
    assert embeddings.calls == []


async def test_malformed_jats_sets_corrupt_file() -> None:
    source, storage = _pmc()
    queue = MemoryQueue(source)
    embeddings = ScriptedEmbeddings()

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=embeddings,
        clock=FrozenClock(),
        pmc_files=FakePmcFiles({_PMCID: b"<article><front>"}),
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.CORRUPT_FILE
    )
    assert embeddings.calls == []


async def test_a_jats_that_is_another_sources_file_fails_as_a_duplicate() -> None:
    source, storage = _pmc()
    other = uploaded_source(admin_id=_ADMIN, marker="same-bytes", file_type=KnowledgeFileType.JATS)
    queue = MemoryQueue(
        source,
        KnowledgeSource(
            **other.model_copy(
                update={
                    "content_hash": hashlib.sha256(_JATS).hexdigest(),
                    "status": SourceStatus.INGESTED,
                }
            ).model_dump()
        ),
    )
    embeddings = ScriptedEmbeddings()

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=embeddings,
        clock=FrozenClock(),
        pmc_files=FakePmcFiles({_PMCID: _JATS}),
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.DUPLICATE_SOURCE
    )
    # The copy it stored is removed, and the source keeps its placeholder, not the other's hash.
    assert storage.objects == {}
    assert queue.sources[source.id].storage_key is None
    assert queue.sources[source.id].content_hash == source.content_hash
    assert embeddings.calls == []


async def test_a_claim_lost_before_the_file_is_recorded_is_gone_and_leaves_no_object() -> None:
    source, storage = _pmc()
    queue = MemoryQueue(source)
    queue.steal_on_record = True
    embeddings = ScriptedEmbeddings()

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=embeddings,
        clock=FrozenClock(),
        pmc_files=FakePmcFiles({_PMCID: _JATS}),
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="gone")
    assert storage.objects == {}
    assert embeddings.calls == []


async def test_a_storage_outage_while_storing_the_jats_fails_the_source() -> None:
    source, storage = _pmc()
    storage.failing.add("put")
    queue = MemoryQueue(source)

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        pmc_files=FakePmcFiles({_PMCID: _JATS}),
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.STORAGE_FAILED
    )
    assert queue.sources[source.id].storage_key is None


async def test_a_discard_that_fails_still_reports_the_duplicate() -> None:
    source, storage = _pmc()
    other = uploaded_source(admin_id=_ADMIN, marker="dup-delete", file_type=KnowledgeFileType.JATS)
    queue = MemoryQueue(
        source,
        KnowledgeSource(
            **other.model_copy(
                update={
                    "content_hash": hashlib.sha256(_JATS).hexdigest(),
                    "status": SourceStatus.INGESTED,
                }
            ).model_dump()
        ),
    )
    storage.failing.add("delete")

    report = await ingestion(
        repository=queue,
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        pmc_files=FakePmcFiles({_PMCID: _JATS}),
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.DUPLICATE_SOURCE
    )


async def test_a_pmc_source_whose_uploader_is_gone_is_not_fetched() -> None:
    source, storage = _pmc(admin_id=None)
    files = FakePmcFiles({_PMCID: _JATS})

    report = await ingestion(
        repository=MemoryQueue(source),
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        pmc_files=files,
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.NO_ACTING_ADMIN
    )
    assert files.requests == []


async def test_an_article_already_stored_is_read_from_storage_not_fetched_again() -> None:
    source, storage = _pmc()
    key = f"sources/{source.id}"
    storage.objects[key] = _JATS
    stored = source.model_copy(update={"storage_key": key})
    files = FakePmcFiles()

    report = await ingestion(
        repository=MemoryQueue(stored),
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        pmc_files=files,
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="ingested", chunks=3)
    assert files.requests == []


async def test_a_jats_source_with_neither_file_nor_pmc_id_is_missing_its_file() -> None:
    source, storage = _pmc()
    no_id = source.model_copy(update={"pmcid": None})
    files = FakePmcFiles({_PMCID: _JATS})

    report = await ingestion(
        repository=MemoryQueue(no_id),
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        pmc_files=files,
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.FILE_MISSING
    )
    assert files.requests == []


async def test_a_pdf_source_with_no_file_is_still_missing_its_file_not_fetched() -> None:
    # Only a JATS source with a PMC ID is fetched; no other type gets a file this way.
    source, storage = _pmc()
    pdf = source.model_copy(update={"file_type": KnowledgeFileType.PDF})
    files = FakePmcFiles({_PMCID: _JATS})

    report = await ingestion(
        repository=MemoryQueue(pdf),
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        pmc_files=files,
    ).ingest_next()

    assert report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.FILE_MISSING
    )
    assert files.requests == []


async def test_an_uploaded_jats_file_uses_the_jats_reader() -> None:
    source, storage = _stored("form-jats", data=_JATS, file_type=KnowledgeFileType.JATS)
    files = FakePmcFiles()

    report = await ingestion(
        repository=MemoryQueue(source),
        storage=storage,
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        pmc_files=files,
    ).ingest_next()

    assert report == IngestReport(source_id=source.id, result="ingested", chunks=3)
    assert files.requests == []
