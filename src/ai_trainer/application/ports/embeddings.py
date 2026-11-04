from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.embeddings import EmbeddingOutcome


class EmbeddingGatewayPort(Protocol):
    """Embeds texts through OpenRouter (ADR-0007's single embeddings adapter). Every call writes
    one `llm_calls` row for `acting_user_id` (ADR-0018); nothing about that user is sent."""

    async def embed_documents(
        self, texts: Sequence[str], *, acting_user_id: UUID
    ) -> EmbeddingOutcome: ...
