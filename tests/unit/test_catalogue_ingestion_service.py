"""The catalogue path of the ingestion worker on fakes (#427): a claim lost mid-run writes nothing
more, and an unexpected error fails the source instead of the worker. The ACs on the real
database are in `tests/integration/test_catalogue_ingestion.py`."""

import asyncio
from collections.abc import Sequence
from uuid import UUID, uuid4

from ai_trainer.application.catalogue_ingestion import CatalogueIngestion
from ai_trainer.domain.catalogue_knowledge import CatalogueText, catalogue_content_hash
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeSource,
    LicenseId,
    LicenseSource,
    SourceLanguage,
    SourceType,
)
from ai_trainer.domain.knowledge_ingestion import IngestFailure, IngestReport
from tests.ingestion_support import (
    AT,
    MODEL,
    FrozenClock,
    MemoryCatalogueChunks,
    MemoryQueue,
    RecordedSleep,
    ScriptedEmbeddings,
    ingestion,
)
from tests.knowledge_support import MemoryStorage

_ADMIN = uuid4()
_TEXTS = (
    (SourceLanguage.EN, CatalogueText(UUID(int=1), "Dumbbell Curl", "Curl the dumbbells up.")),
    (SourceLanguage.EN, CatalogueText(UUID(int=2), "Squat", "Sit back and stand up.")),
)


def _catalogue_source() -> KnowledgeSource:
    return KnowledgeSource(
        id=uuid4(),
        title="Exercise catalogue",
        author=None,
        source_type=SourceType.CATALOGUE,
        evidence_tier=EvidenceTier.CATALOGUE,
        language=SourceLanguage.EN,
        sports=("gym",),
        topics=(),
        license_note="MIT licence",
        source_url=None,
        license_id=LicenseId.OTHER,
        license_source=LicenseSource.ADMIN,
        content_hash=catalogue_content_hash(SourceLanguage.EN),
        file_type=None,
        file_name=None,
        size_bytes=None,
        storage_key=None,
        uploaded_by_user_id=_ADMIN,
        uploaded_at=AT,
    )


def _service(
    chunks: MemoryCatalogueChunks, embeddings: ScriptedEmbeddings, *, batch_size: int = 1
) -> CatalogueIngestion:
    return CatalogueIngestion(
        repository=chunks,
        embeddings=embeddings,
        clock=FrozenClock(),
        sleep=RecordedSleep(),
        batch_size=batch_size,
        max_attempts=3,
        model=MODEL,
    )


async def test_a_claim_lost_during_a_batch_stops_before_the_next() -> None:
    lost = asyncio.Event()

    async def _lose() -> None:
        lost.set()

    chunks = MemoryCatalogueChunks(*_TEXTS)
    embeddings = ScriptedEmbeddings(during=_lose)

    outcome = await _service(chunks, embeddings).ingest(_catalogue_source(), uuid4(), lost)

    assert outcome == "gone"
    assert len(embeddings.calls) == 1
    assert chunks.finished is None


async def test_a_claim_lost_during_the_last_batch_never_finishes() -> None:
    lost = asyncio.Event()

    async def _lose() -> None:
        lost.set()

    chunks = MemoryCatalogueChunks(*_TEXTS)

    outcome = await _service(chunks, ScriptedEmbeddings(during=_lose), batch_size=2).ingest(
        _catalogue_source(), uuid4(), lost
    )

    assert outcome == "gone"
    assert chunks.finished is None


async def test_a_source_gone_at_a_write_or_the_finish_is_gone() -> None:
    at_write = MemoryCatalogueChunks(*_TEXTS)
    at_write.gone_after_writes = 0
    at_finish = MemoryCatalogueChunks(*_TEXTS)
    at_finish.gone_after_writes = 2

    first = await _service(at_write, ScriptedEmbeddings()).ingest(
        _catalogue_source(), uuid4(), asyncio.Event()
    )
    second = await _service(at_finish, ScriptedEmbeddings()).ingest(
        _catalogue_source(), uuid4(), asyncio.Event()
    )

    assert (first, second) == ("gone", "gone")
    assert at_finish.finished is None


class _Broken(MemoryCatalogueChunks):
    async def texts(self, language: SourceLanguage) -> Sequence[CatalogueText]:
        raise RuntimeError("boom")


async def test_the_worker_reports_a_catalogue_source_gone_or_failed() -> None:
    source = _catalogue_source()
    gone = MemoryCatalogueChunks(*_TEXTS)
    gone.gone_after_writes = 0

    gone_report = await ingestion(
        repository=MemoryQueue(source),
        storage=MemoryStorage(),
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        catalogue=gone,
    ).ingest_next()
    broken_queue = MemoryQueue(source)
    broken_report = await ingestion(
        repository=broken_queue,
        storage=MemoryStorage(),
        embeddings=ScriptedEmbeddings(),
        clock=FrozenClock(),
        catalogue=_Broken(),
    ).ingest_next()

    assert gone_report == IngestReport(source_id=source.id, result="gone")
    assert broken_report == IngestReport(
        source_id=source.id, result="failed", reason=IngestFailure.INGEST_ERROR
    )
    assert broken_queue.sources[source.id].error == "ingest_error"
