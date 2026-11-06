"""Fakes for the ingestion worker's tests (ADR-0036, #423): an embedding gateway scripted per
call, a clock that moves only when told, and a recorded sleep."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from ai_trainer.application.catalogue_ingestion import CatalogueIngestion
from ai_trainer.application.knowledge_ingestion import KnowledgeIngestion
from ai_trainer.application.pmc_source_file import PmcSourceFile
from ai_trainer.application.ports.embeddings import EmbeddingGatewayPort
from ai_trainer.application.ports.knowledge import (
    CatalogueChunksRepositoryPort,
    KnowledgeIngestionRepositoryPort,
)
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.application.ports.pmc import PmcArticleFilesPort
from ai_trainer.domain.catalogue_knowledge import (
    CatalogueChunkState,
    CatalogueText,
    EmbeddedCatalogueChunk,
)
from ai_trainer.domain.embeddings import Embedded, EmbeddingOutcome
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeFileType,
    KnowledgeSource,
    LicenseId,
    LicenseSource,
    NewKnowledgeSource,
    SourceLanguage,
    SourceStatus,
    SourceType,
)
from ai_trainer.domain.knowledge_ingestion import EmbeddedChunk, FileRecord, IngestFailure
from ai_trainer.domain.pmc import PmcNotFound, PmcUnavailable, pmc_placeholder_hash
from ai_trainer.knowledge.chunker import Chunker
from ai_trainer.knowledge.readers import DocumentReaders

DIMENSIONS = 1024
MODEL = "qwen/qwen3-embedding-8b"
AT = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)

# Three sections, each its own chunk at the test budget, and references left out.
GUIDE = """---
title: Deload guide
---

# Deload guide

## Why

{why}

## Timing

{timing}

## Volume

{volume}

## References

Smith J. Deloading. 2020.
"""


def guide_markdown(marker: str) -> bytes:
    """A unique guide per test: `content_hash` is unique and tests share one database."""
    why = " ".join(["Fatigue", "masks", "fitness."] * 10) + f" {marker}"
    timing = " ".join(["Deload", "every", "fourth", "week."] * 8)
    volume = " ".join(["Halve", "the", "sets,", "keep", "the", "load."] * 6)
    return GUIDE.format(why=why, timing=timing, volume=volume).encode()


class FrozenClock:
    def __init__(self, at: datetime = AT) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, delta: timedelta) -> None:
        self.at += delta


class ScriptedEmbeddings:
    """Implements `EmbeddingGatewayPort`. Each call takes the next scripted outcome, or embeds
    when the script has run out; `during` runs inside each call, before it answers."""

    def __init__(
        self,
        *script: EmbeddingOutcome,
        during: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.script = list(script)
        self.during = during
        self.calls: list[tuple[list[str], UUID]] = []

    async def embed_documents(
        self, texts: Sequence[str], *, acting_user_id: UUID
    ) -> EmbeddingOutcome:
        self.calls.append((list(texts), acting_user_id))
        if self.during is not None:
            await self.during()
        if self.script:
            return self.script.pop(0)
        return Embedded(
            vectors=tuple(tuple([0.01 * (i + 1)] * DIMENSIONS) for i in range(len(texts))),
            model=MODEL,
        )


class RecordedSleep:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        await asyncio.sleep(0)


def uploaded_source(
    *,
    admin_id: UUID | None,
    marker: str,
    file_type: KnowledgeFileType = KnowledgeFileType.MARKDOWN,
    status: SourceStatus = SourceStatus.UPLOADED,
    language: SourceLanguage = SourceLanguage.EN,
    uploaded_at: datetime = AT,
) -> NewKnowledgeSource:
    source_id = uuid4()
    licensed = status not in (SourceStatus.DETECTING, SourceStatus.PROPOSED)
    return NewKnowledgeSource(
        id=source_id,
        title=f"Deload guide {marker}",
        author="M. Kowalska",
        source_type=SourceType.ARTICLE,
        evidence_tier=EvidenceTier.ARTICLE_OR_TRANSCRIPT,
        language=language,
        sports=("gym",),
        topics=("deload",),
        license_note=None,
        source_url=None,
        license_id=LicenseId.CC_BY if licensed else None,
        license_source=LicenseSource.ADMIN if licensed else None,
        content_hash=uuid4().hex * 2,
        file_type=file_type,
        file_name=f"{marker}.md",
        size_bytes=100,
        storage_key=f"sources/{source_id}",
        status=status,
        uploaded_by_user_id=admin_id,
        uploaded_at=uploaded_at,
    )


def ingestion(
    *,
    repository: KnowledgeIngestionRepositoryPort,
    storage: ObjectStoragePort,
    embeddings: EmbeddingGatewayPort,
    clock: FrozenClock,
    sleep: RecordedSleep | None = None,
    batch_size: int = 2,
    max_attempts: int = 3,
    lease: timedelta = timedelta(minutes=5),
    catalogue: CatalogueChunksRepositoryPort | None = None,
    pmc_files: PmcArticleFilesPort | None = None,
    chunker: Chunker | None = None,
) -> KnowledgeIngestion:
    sleep = sleep or RecordedSleep()
    return KnowledgeIngestion(
        repository=repository,
        storage=storage,
        pmc=PmcSourceFile(
            files=pmc_files or FakePmcFiles(), storage=storage, repository=repository
        ),
        readers=DocumentReaders(),
        chunker=chunker or Chunker(max_words=40, min_words=5),
        embeddings=embeddings,
        clock=clock,
        sleep=sleep,
        batch_size=batch_size,
        max_attempts=max_attempts,
        lease=lease,
        catalogue=CatalogueIngestion(
            repository=catalogue or MemoryCatalogueChunks(),
            embeddings=embeddings,
            clock=clock,
            sleep=sleep,
            batch_size=batch_size,
            max_attempts=max_attempts,
            model=MODEL,
        ),
    )


class MemoryCatalogueChunks:
    """Implements `CatalogueChunksRepositoryPort` in memory, for any claim (#427)."""

    def __init__(self, *texts: tuple[SourceLanguage, CatalogueText]) -> None:
        self.texts_by_language = list(texts)
        self.chunks: dict[UUID, EmbeddedCatalogueChunk] = {}
        self.models: dict[UUID, str] = {}
        self.writes = 0
        self.finished: frozenset[UUID] | None = None
        self.gone_after_writes: int | None = None

    async def texts(self, language: SourceLanguage) -> Sequence[CatalogueText]:
        return [text for lang, text in self.texts_by_language if lang is language]

    async def chunk_states(self, source_id: UUID) -> Sequence[CatalogueChunkState]:
        return [
            CatalogueChunkState(
                exercise_id,
                chunk.item.chunk.text,
                chunk.item.chunk.heading_path,
                self.models[exercise_id],
            )
            for exercise_id, chunk in self.chunks.items()
        ]

    async def write_batch(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        chunks: Sequence[EmbeddedCatalogueChunk],
        model: str,
        at: datetime,
    ) -> bool:
        if self.gone_after_writes is not None and self.writes >= self.gone_after_writes:
            return False
        self.writes += 1
        for chunk in chunks:
            self.chunks[chunk.item.exercise_id] = chunk
            self.models[chunk.item.exercise_id] = model
        return True

    async def finish(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        keep: frozenset[UUID],
        model: str,
        ingested_at: datetime,
    ) -> bool:
        if self.gone_after_writes is not None and self.writes >= self.gone_after_writes:
            return False
        self.finished = keep
        return True


class MemoryQueue:
    """Implements `KnowledgeIngestionRepositoryPort` in memory, one claim at a time."""

    def __init__(self, *sources: KnowledgeSource) -> None:
        self.sources = {source.id: source for source in sources}
        self.claims: dict[UUID, UUID] = {}
        self.leases: dict[UUID, datetime] = {}
        self.chunks: dict[UUID, list[EmbeddedChunk]] = {}
        self.models: dict[UUID, str] = {}
        self.heartbeats = 0
        self.steal_on_heartbeat = False
        self.steal_on_record = False

    async def claim_next(
        self, *, claim_id: UUID, now: datetime, lease_until: datetime
    ) -> KnowledgeSource | None:
        for source in self.sources.values():
            if source.status is SourceStatus.UPLOADED:
                self.sources[source.id] = source.model_copy(
                    update={"status": SourceStatus.INGESTING}
                )
                self.claims[source.id] = claim_id
                self.leases[source.id] = lease_until
                return self.sources[source.id]
        return None

    def _holds(self, source_id: UUID, claim_id: UUID) -> bool:
        return source_id in self.sources and self.claims.get(source_id) == claim_id

    async def heartbeat(self, source_id: UUID, *, claim_id: UUID, lease_until: datetime) -> bool:
        self.heartbeats += 1
        if self.steal_on_heartbeat:
            self.claims[source_id] = uuid4()
        if not self._holds(source_id, claim_id):
            return False
        self.leases[source_id] = lease_until
        return True

    async def finish(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        chunks: Sequence[EmbeddedChunk],
        model: str,
        ingested_at: datetime,
    ) -> bool:
        if not self._holds(source_id, claim_id):
            return False
        self.chunks[source_id] = list(chunks)
        self.models[source_id] = model
        self.sources[source_id] = self.sources[source_id].model_copy(
            update={"status": SourceStatus.INGESTED, "ingested_at": ingested_at}
        )
        return True

    async def fail(self, source_id: UUID, *, claim_id: UUID, reason: IngestFailure) -> bool:
        if not self._holds(source_id, claim_id):
            return False
        self.sources[source_id] = self.sources[source_id].model_copy(
            update={"status": SourceStatus.FAILED, "error": reason.value}
        )
        return True

    async def record_file(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        storage_key: str,
        content_hash: str,
        size_bytes: int,
        file_name: str,
    ) -> FileRecord:
        if self.steal_on_record:
            self.claims[source_id] = uuid4()
        if not self._holds(source_id, claim_id):
            return FileRecord.GONE
        if any(s.content_hash == content_hash for s in self.sources.values() if s.id != source_id):
            return FileRecord.DUPLICATE
        self.sources[source_id] = self.sources[source_id].model_copy(
            update={
                "storage_key": storage_key,
                "content_hash": content_hash,
                "size_bytes": size_bytes,
                "file_name": file_name,
            }
        )
        return FileRecord.RECORDED


class FakePmcFiles:
    """Implements `PmcArticleFilesPort`: each PMC ID answers with what it was given, and a
    request for an ID it was not given is not found."""

    def __init__(self, files: Mapping[str, bytes | PmcNotFound | PmcUnavailable] | None = None):
        self.files = dict(files or {})
        self.requests: list[str] = []

    async def fetch_jats(self, pmcid: str) -> bytes | PmcNotFound | PmcUnavailable:
        self.requests.append(pmcid)
        return self.files.get(pmcid, PmcNotFound())


def pmc_source(
    *, admin_id: UUID | None, pmcid: str, status: SourceStatus = SourceStatus.UPLOADED
) -> NewKnowledgeSource:
    """A PMC article as staging stores it and approval leaves it: no file yet, the placeholder
    hash (#458)."""
    return NewKnowledgeSource(
        id=uuid4(),
        title=f"Article {pmcid}",
        author=None,
        source_type=SourceType.RESEARCH_PAPER,
        evidence_tier=EvidenceTier.PEER_REVIEWED,
        language=SourceLanguage.EN,
        sports=("gym",),
        topics=("hypertrophy",),
        license_note=None,
        source_url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
        pmcid=pmcid,
        license_id=LicenseId.CC_BY,
        license_source=LicenseSource.PMC,
        attribution="Source: NLM PMC.",
        content_hash=pmc_placeholder_hash(pmcid),
        file_type=KnowledgeFileType.JATS,
        file_name=None,
        size_bytes=None,
        storage_key=None,
        status=status,
        uploaded_by_user_id=admin_id,
        uploaded_at=AT,
    )
