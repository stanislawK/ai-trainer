"""One batch of document embeddings with backoff on 429, shared by the file and catalogue paths
of the ingestion worker (ADR-0036, #423, #427)."""

from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from ai_trainer.application.ports.embeddings import EmbeddingGatewayPort
from ai_trainer.domain.embeddings import Embedded, EmbeddingFailed, EmbeddingFailure, RateLimited
from ai_trainer.domain.knowledge_document import EMBEDDING_DIMENSIONS
from ai_trainer.domain.knowledge_ingestion import IngestFailure

# 1 s, doubling, at most 30 s, when a 429 doesn't say how long to wait; a wait it names is kept
# between 0 and the same cap, so one answer can't stall the queue.
_BACKOFF_START_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 30.0
_EMBEDDING_FAILURES = {
    EmbeddingFailure.ERROR: IngestFailure.EMBEDDING_FAILED,
    EmbeddingFailure.TIMEOUT: IngestFailure.EMBEDDING_TIMEOUT,
    EmbeddingFailure.WRONG_SHAPE: IngestFailure.WRONG_SHAPE,
}


async def embed_batch(
    embeddings: EmbeddingGatewayPort,
    texts: Sequence[str],
    *,
    acting_user: UUID,
    sleep: Callable[[float], Awaitable[None]],
    max_attempts: int,
) -> Embedded | IngestFailure:
    """The batch's vectors, each checked for its length, or why the batch failed."""
    wait = _BACKOFF_START_SECONDS
    for attempt in range(1, max_attempts + 1):
        outcome = await embeddings.embed_documents(texts, acting_user_id=acting_user)
        match outcome:
            case Embedded():
                if any(len(vector) != EMBEDDING_DIMENSIONS for vector in outcome.vectors):
                    return IngestFailure.WRONG_SHAPE
                return outcome
            case EmbeddingFailed(reason=reason):
                return _EMBEDDING_FAILURES[reason]
            case RateLimited(retry_after_seconds=retry_after):
                if attempt == max_attempts:
                    break
                await sleep(
                    wait
                    if retry_after is None
                    else min(max(retry_after, 0.0), _BACKOFF_CAP_SECONDS)
                )
                wait = min(wait * 2, _BACKOFF_CAP_SECONDS)
    return IngestFailure.RATE_LIMITED
