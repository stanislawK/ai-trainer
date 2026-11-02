from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.knowledge import KnowledgeSource, NewKnowledgeSource


class KnowledgeSourcesRepositoryPort(Protocol):
    """The knowledge base's manifest (ADR-0033). Shared reference data, not an athlete's: only
    an admin reaches it, so no method takes a `user_id` to scope by."""

    async def list_all(self) -> Sequence[KnowledgeSource]:
        """Newest upload first."""
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
