from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.embeddings import EmbeddingOutcome, QueryTask


class EmbeddingGatewayPort(Protocol):
    """Embeds texts through OpenRouter (ADR-0007's single embeddings adapter). Every call writes
    one `llm_calls` row for `acting_user_id` (ADR-0018); nothing about that user is sent."""

    async def embed_documents(
        self, texts: Sequence[str], *, acting_user_id: UUID
    ) -> EmbeddingOutcome: ...


class QueryEmbeddingPort(Protocol):
    """Embeds one search query with the model's query instruction (ADR-0033, #424). The call
    writes one `llm_calls` row for `acting_user_id`, the athlete who searched; only the query
    text is sent."""

    async def embed_query(
        self, text: str, *, acting_user_id: UUID, task: QueryTask = QueryTask.QUESTION
    ) -> EmbeddingOutcome:
        """`task` picks the instruction line: a question, or a description of a gym movement
        (#429)."""
        ...
