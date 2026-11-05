"""The ingestion worker (ADR-0036, PRD B7, #423): file → document → chunks → embeddings → stored.

It runs outside the web process (`ai-trainer ingest-worker`, in the `ingest` container from
#462). A source's status row is the queue:
- a claim takes an `uploaded` source, or one whose lease lapsed, and only those (invariant 1);
- a heartbeat renews the lease while the source is read, and a heartbeat that finds the claim
  gone stops the work before the next embedding call;
- chunks are written only at the end, in one transaction that replaces any old ones. A worker
  killed midway leaves no partial set, and the next one takes the source over once the lease
  lapses;
- a source deleted midway is "gone": nothing is written for it.

Parsing and chunking are CPU-bound, so they run in a thread and the heartbeat keeps beating.
Embedding calls go in batches, each a call with its own `llm_calls` row, with backoff on 429.
They are outside ADR-0029's per-user budget, bounded by the worker's concurrency of 1 instead.

A `catalogue` source has no file: `CatalogueIngestion` embeds only its changed texts and writes
them batch by batch, under the same claim and heartbeat (ADR-0036's #427 amendment).
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from ai_trainer.application.catalogue_ingestion import CatalogueIngestion
from ai_trainer.application.embedding_batches import embed_batch
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.embeddings import EmbeddingGatewayPort
from ai_trainer.application.ports.knowledge import (
    ChunkerPort,
    DocumentReadersPort,
    KnowledgeIngestionRepositoryPort,
)
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.domain.knowledge import KnowledgeFileType, KnowledgeSource, SourceType
from ai_trainer.domain.knowledge_document import (
    NewKnowledgeChunk,
    NoReader,
    Unreadable,
    UnreadableReason,
    embedding_input,
)
from ai_trainer.domain.knowledge_ingestion import EmbeddedChunk, IngestFailure, IngestReport
from ai_trainer.domain.object_storage import ObjectNotFoundError, ObjectStorageError

_UNREADABLE = {
    UnreadableReason.NOT_UTF8: IngestFailure.NOT_UTF8,
    UnreadableReason.NO_TEXT: IngestFailure.NO_TEXT,
    UnreadableReason.NO_TEXT_LAYER: IngestFailure.NO_TEXT_LAYER,
    UnreadableReason.CORRUPT: IngestFailure.CORRUPT_FILE,
    UnreadableReason.ENCRYPTED: IngestFailure.ENCRYPTED_FILE,
}


logger = logging.getLogger(__name__)


class _ClaimLostError(Exception):
    """The source was deleted or taken over while it was read."""


@dataclass(frozen=True, slots=True)
class _Embeddings:
    chunks: tuple[EmbeddedChunk, ...]
    model: str


class KnowledgeIngestion:
    def __init__(
        self,
        *,
        repository: KnowledgeIngestionRepositoryPort,
        storage: ObjectStoragePort,
        readers: DocumentReadersPort,
        chunker: ChunkerPort,
        embeddings: EmbeddingGatewayPort,
        clock: ClockPort,
        sleep: Callable[[float], Awaitable[None]],
        batch_size: int,
        max_attempts: int,
        lease: timedelta,
        catalogue: CatalogueIngestion,
    ) -> None:
        self._repository = repository
        self._catalogue = catalogue
        self._storage = storage
        self._readers = readers
        self._chunker = chunker
        self._embeddings = embeddings
        self._clock = clock
        self._sleep = sleep
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease = lease

    async def run(
        self, *, poll_seconds: float, on_report: Callable[[IngestReport], Awaitable[None]]
    ) -> None:
        """Ingests sources as they arrive, until cancelled."""
        while True:
            report = await self.ingest_next()
            if report is None:
                await self._sleep(poll_seconds)
            else:
                await on_report(report)

    async def drain(self) -> list[IngestReport]:
        """Ingests every source waiting now, then returns (`ingest-worker --once`)."""
        reports: list[IngestReport] = []
        while (report := await self.ingest_next()) is not None:
            reports.append(report)
        return reports

    async def ingest_next(self) -> IngestReport | None:
        claim_id = uuid4()
        now = self._clock.now()
        source = await self._repository.claim_next(
            claim_id=claim_id, now=now, lease_until=now + self._lease
        )
        if source is None:
            return None
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(source.id, claim_id, lost))
        try:
            return await self._ingest(source, claim_id, lost)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, source_id: UUID, claim_id: UUID, lost: asyncio.Event) -> None:
        interval = self._lease.total_seconds() / 3
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._repository.heartbeat(
                    source_id, claim_id=claim_id, lease_until=self._clock.now() + self._lease
                )
            except Exception as exc:
                # A blip: the next beat tries again, and `finish` checks the claim regardless.
                logger.warning("ingest heartbeat failed: %s", type(exc).__name__)
                continue
            if not renewed:
                lost.set()
                return

    async def _ingest(
        self, source: KnowledgeSource, claim_id: UUID, lost: asyncio.Event
    ) -> IngestReport:
        if source.source_type is SourceType.CATALOGUE:
            return await self._ingest_catalogue(source, claim_id, lost)
        return await self._ingest_file(source, claim_id, lost)

    async def _fail(
        self, source: KnowledgeSource, claim_id: UUID, reason: IngestFailure
    ) -> IngestReport:
        failed = await self._repository.fail(source.id, claim_id=claim_id, reason=reason)
        if not failed:
            return IngestReport(source_id=source.id, result="gone")
        return IngestReport(source_id=source.id, result="failed", reason=reason)

    async def _ingest_catalogue(
        self, source: KnowledgeSource, claim_id: UUID, lost: asyncio.Event
    ) -> IngestReport:
        try:
            outcome = await self._catalogue.ingest(source, claim_id, lost)
        except Exception as exc:
            logger.exception("catalogue ingest failed unexpectedly: %s", type(exc).__name__)
            outcome = IngestFailure.INGEST_ERROR
        if isinstance(outcome, IngestFailure):
            return await self._fail(source, claim_id, outcome)
        if outcome == "gone":
            return IngestReport(source_id=source.id, result="gone")
        return IngestReport(source_id=source.id, result="ingested", chunks=outcome)

    async def _ingest_file(
        self, source: KnowledgeSource, claim_id: UUID, lost: asyncio.Event
    ) -> IngestReport:
        try:
            outcome = await self._read_and_embed(source, lost)
        except _ClaimLostError:
            return IngestReport(source_id=source.id, result="gone")
        except Exception as exc:
            # Never the file's content: only what kind of failure it was.
            logger.exception("ingest failed unexpectedly: %s", type(exc).__name__)
            outcome = IngestFailure.INGEST_ERROR
        if isinstance(outcome, IngestFailure):
            return await self._fail(source, claim_id, outcome)
        finished = await self._repository.finish(
            source.id,
            claim_id=claim_id,
            chunks=outcome.chunks,
            model=outcome.model,
            ingested_at=self._clock.now(),
        )
        if not finished:
            return IngestReport(source_id=source.id, result="gone")
        return IngestReport(source_id=source.id, result="ingested", chunks=len(outcome.chunks))

    async def _read_and_embed(
        self, source: KnowledgeSource, lost: asyncio.Event
    ) -> _Embeddings | IngestFailure:
        acting_user = source.uploaded_by_user_id
        if acting_user is None:
            return IngestFailure.NO_ACTING_ADMIN
        if source.storage_key is None or source.file_type is None:
            return IngestFailure.FILE_MISSING
        try:
            data = await self._storage.get(source.storage_key)
        except ObjectNotFoundError:
            return IngestFailure.FILE_MISSING
        except ObjectStorageError:
            return IngestFailure.STORAGE_FAILED
        chunks = await asyncio.to_thread(self._chunks, source.file_type, data)
        if isinstance(chunks, IngestFailure):
            return chunks
        return await self._embed(source, chunks, acting_user, lost)

    def _chunks(
        self, file_type: KnowledgeFileType, data: bytes
    ) -> Sequence[NewKnowledgeChunk] | IngestFailure:
        document = self._readers.parse(file_type, data)
        if isinstance(document, NoReader):
            return IngestFailure.NO_READER
        if isinstance(document, Unreadable):
            return _UNREADABLE[document.reason]
        chunks = self._chunker.chunk(document)
        return chunks if chunks else IngestFailure.NO_TEXT

    async def _embed(
        self,
        source: KnowledgeSource,
        chunks: Sequence[NewKnowledgeChunk],
        acting_user: UUID,
        lost: asyncio.Event,
    ) -> _Embeddings | IngestFailure:
        embedded: list[EmbeddedChunk] = []
        model = ""
        for start in range(0, len(chunks), self._batch_size):
            if lost.is_set():
                raise _ClaimLostError
            batch = chunks[start : start + self._batch_size]
            outcome = await embed_batch(
                self._embeddings,
                [embedding_input(source.title, chunk) for chunk in batch],
                acting_user=acting_user,
                sleep=self._sleep,
                max_attempts=self._max_attempts,
            )
            if isinstance(outcome, IngestFailure):
                return outcome
            embedded.extend(
                EmbeddedChunk(chunk=chunk, embedding=vector)
                for chunk, vector in zip(batch, outcome.vectors, strict=True)
            )
            model = outcome.model
        if lost.is_set():
            raise _ClaimLostError
        return _Embeddings(chunks=tuple(embedded), model=model)
