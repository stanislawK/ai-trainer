from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.knowledge import KnowledgeFileType, KnowledgeSource, NewKnowledgeSource
from ai_trainer.domain.knowledge_document import (
    KnowledgeDocument,
    NewKnowledgeChunk,
    NoReader,
    Unreadable,
)
from ai_trainer.domain.knowledge_ingestion import EmbeddedChunk, IngestFailure


class KnowledgeSourcesRepositoryPort(Protocol):
    """The knowledge base's manifest (ADR-0033). Shared reference data, not an athlete's: only
    an admin reaches it, so no method takes a `user_id` to scope by."""

    async def list_all(self) -> Sequence[KnowledgeSource]:
        """Newest upload first."""
        ...

    async def list_page(self, *, after: UUID | None, limit: int) -> Sequence[KnowledgeSource]:
        """Up to `limit` sources ordered by `id`, those after `after` when it is set (#457). A
        source present for the whole walk is listed once; one added during it may sort before
        the cursor and be missed, which staging tolerates: its identifiers and hash are unique,
        so a duplicate it stages again is refused."""
        ...

    async def get(self, source_id: UUID) -> KnowledgeSource | None: ...

    async def get_by_hash(self, content_hash: str) -> KnowledgeSource | None: ...

    async def add(self, source: NewKnowledgeSource) -> KnowledgeSource:
        """Raises `DuplicateSourceError` when a source with its content hash is stored."""
        ...

    async def delete(self, source_id: UUID) -> bool:
        """False when there was no such source."""
        ...

    async def queue_reingest(
        self, source_id: UUID, *, requested_by: UUID
    ) -> KnowledgeSource | None:
        """Sets an `ingested` or `failed` source back to `uploaded`, clearing its error and
        ingest time. None, changing nothing, for any other status or an unknown source."""
        ...


class KnowledgeIngestionRepositoryPort(Protocol):
    """The worker's queue is the sources' status row (ADR-0036, knowledge-base.md rec 10). A claim
    is a `claim_id` with a lease; every write after the claim names it, so a worker whose lease
    lapsed and was taken over, or whose source was deleted, changes nothing."""

    async def claim_next(
        self, *, claim_id: UUID, now: datetime, lease_until: datetime
    ) -> KnowledgeSource | None:
        """Takes the oldest `uploaded` source, or an `ingesting` one whose lease lapsed before
        `now`, with `FOR UPDATE SKIP LOCKED`, and marks it `ingesting` under `claim_id`. Never
        a `proposed`, `detecting` or `rejected` source (ADR-0036 invariant 1). None when there is
        nothing to do."""
        ...

    async def heartbeat(self, source_id: UUID, *, claim_id: UUID, lease_until: datetime) -> bool:
        """Extends the lease. False when the source is gone or no longer this claim's."""
        ...

    async def finish(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        chunks: Sequence[EmbeddedChunk],
        model: str,
        ingested_at: datetime,
    ) -> bool:
        """In one transaction: locks the source, replaces its chunks and sets `ingested`. False,
        writing nothing, when the source is gone or no longer this claim's."""
        ...

    async def fail(self, source_id: UUID, *, claim_id: UUID, reason: IngestFailure) -> bool:
        """Sets `failed` with `reason`, keeping any chunks an earlier ingest wrote. False when the
        source is gone or no longer this claim's."""
        ...


class DocumentReadersPort(Protocol):
    def parse(
        self, file_type: KnowledgeFileType, data: bytes
    ) -> KnowledgeDocument | Unreadable | NoReader: ...


class ChunkerPort(Protocol):
    def chunk(self, document: KnowledgeDocument) -> Sequence[NewKnowledgeChunk]: ...
