from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.catalogue_knowledge import (
    CatalogueChunkState,
    CatalogueText,
    EmbeddedCatalogueChunk,
)
from ai_trainer.domain.knowledge import (
    KnowledgeFileType,
    KnowledgeSource,
    NewKnowledgeSource,
    SourceLanguage,
    SourceStatus,
)
from ai_trainer.domain.knowledge_document import (
    KnowledgeDocument,
    NewKnowledgeChunk,
    NoReader,
    Unreadable,
)
from ai_trainer.domain.knowledge_ingestion import EmbeddedChunk, FileRecord, IngestFailure
from ai_trainer.domain.knowledge_search import KnowledgeCandidates, KnowledgeFilters
from ai_trainer.domain.source_template import LicenceTerms


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

    async def find_by_identifiers(
        self,
        *,
        pmcid: str | None = None,
        doi: str | None = None,
        pmid: str | None = None,
        isbn: str | None = None,
    ) -> KnowledgeSource | None:
        """The oldest source with any of the given identifiers; None when none is given."""
        ...

    async def add_if_new(self, source: NewKnowledgeSource) -> KnowledgeSource | None:
        """Stores a staged source (#458). None, changing nothing, when its hash or one of its
        identifiers is a stored source's already."""
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

    async def get_many(self, source_ids: Collection[UUID]) -> Sequence[KnowledgeSource]:
        """The sources with these IDs that exist, in no particular order."""
        ...

    async def decide_proposals(
        self, source_ids: Collection[UUID], *, status: SourceStatus, decided_by: UUID
    ) -> Sequence[UUID]:
        """In one statement, sets each `proposed` source that has a licence to `status`, which is
        `uploaded` or `rejected` (ADR-0036, #461). Approving records `decided_by` as the source's
        acting admin. Returns the IDs it changed; any other is left as it was."""
        ...

    async def set_licence(self, source_id: UUID, terms: LicenceTerms) -> KnowledgeSource | None:
        """Writes an admin's licence on a `proposed` source that has none, clearing what detection
        left (the #454 amendment). None, changing nothing, for any other status, a source that
        has a licence, or an unknown source."""
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
        """Gives a PMC source the JATS file the worker fetched for it (#463): its key, name and
        size, and the file's hash in place of the placeholder staging stored (ADR-0036, the #458
        amendment). `DUPLICATE`, changing nothing, when another source has that hash; `GONE`,
        changing nothing, when the source is gone or no longer this claim's."""
        ...


class CatalogueSourcesPort(Protocol):
    """The catalogue's two sources, one per language (ADR-0036's #427 amendment)."""

    async def queue(
        self,
        *,
        attribution: str | None,
        admin_emails: Sequence[str],
        now: datetime,
    ) -> int:
        """Adds a missing source as `uploaded`. An existing one goes back to `uploaded` when it
        `failed`, or when any of its language's texts has no chunk or a changed one, even while
        `ingesting`, whose worker then loses its claim. It reads what is stored, not what this
        import wrote. Each queued source's acting user is the oldest `active` user whose email is
        in `admin_emails`, or None when there is none. Returns how many sources were queued."""
        ...


class CatalogueChunksRepositoryPort(Protocol):
    """A catalogue source's chunks, written batch by batch under the worker's claim, unlike a
    file source's single write: each chunk stands alone (ADR-0036's #427 amendment)."""

    async def texts(self, language: SourceLanguage) -> Sequence[CatalogueText]:
        """Every catalogue exercise's text in `language`, never an athlete's custom one."""
        ...

    async def chunk_states(self, source_id: UUID) -> Sequence[CatalogueChunkState]: ...

    async def write_batch(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        chunks: Sequence[EmbeddedCatalogueChunk],
        model: str,
        at: datetime,
    ) -> bool:
        """Replaces each exercise's chunk, keeping its ordinal, and adds new ones after the
        last. False, writing nothing, when the source is gone or no longer this claim's."""
        ...

    async def finish(
        self,
        source_id: UUID,
        *,
        claim_id: UUID,
        keep: frozenset[UUID],
        model: str,
        ingested_at: datetime,
    ) -> bool:
        """Removes the chunks of exercises not in `keep` and sets `ingested` with the count.
        False, writing nothing, when the source is gone or no longer this claim's."""
        ...


class DocumentReadersPort(Protocol):
    def parse(
        self, file_type: KnowledgeFileType, data: bytes
    ) -> KnowledgeDocument | Unreadable | NoReader: ...


class ChunkerPort(Protocol):
    def chunk(self, document: KnowledgeDocument) -> Sequence[NewKnowledgeChunk]: ...


class KnowledgeSearchPort(Protocol):
    """The two legs of `search_knowledge` (ADR-0033, #424). Chunks are shared reference data,
    so nothing here is scoped by user."""

    async def candidates(
        self,
        *,
        vector: Sequence[float],
        query: str,
        filters: KnowledgeFilters,
        depth: int,
    ) -> KnowledgeCandidates:
        """Up to `depth` chunks nearest `vector` by cosine distance, and up to `depth` chunks
        matching `query`'s words, each best first, both under `filters`."""
        ...
