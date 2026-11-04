"""Fakes for the ingestion worker's tests (ADR-0036, #423): an embedding gateway scripted per
call, a clock that moves only when told, and a recorded sleep."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from ai_trainer.application.knowledge_ingestion import KnowledgeIngestion
from ai_trainer.application.ports.embeddings import EmbeddingGatewayPort
from ai_trainer.application.ports.knowledge import KnowledgeIngestionRepositoryPort
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.embeddings import Embedded, EmbeddingOutcome
from ai_trainer.domain.knowledge import (
    EvidenceTier,
    KnowledgeFileType,
    LicenseId,
    LicenseSource,
    NewKnowledgeSource,
    SourceLanguage,
    SourceStatus,
    SourceType,
)
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
) -> KnowledgeIngestion:
    return KnowledgeIngestion(
        repository=repository,
        storage=storage,
        readers=DocumentReaders(),
        chunker=Chunker(max_words=40, min_words=5),
        embeddings=embeddings,
        clock=clock,
        sleep=sleep or RecordedSleep(),
        batch_size=batch_size,
        max_attempts=max_attempts,
        lease=lease,
    )
