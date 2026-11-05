"""The ingestion worker's path for the exercise catalogue's sources (ADR-0033 *Catalogue texts*,
ADR-0036's #427 amendment, PRD B7).

A catalogue source has no file. The worker reads the language's `exercise_texts` and the source's
chunks, and embeds only the texts whose chunk is missing, changed or from another model. Each batch
is written as soon as it is embedded, so a failed batch keeps the ones before it, and the next run
embeds only the rest. The claim and its heartbeat are `KnowledgeIngestion`'s, as for a file.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID

from ai_trainer.application.embedding_batches import embed_batch
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.embeddings import EmbeddingGatewayPort
from ai_trainer.application.ports.knowledge import CatalogueChunksRepositoryPort
from ai_trainer.domain.catalogue_knowledge import (
    EmbeddedCatalogueChunk,
    kept_exercises,
    stale_chunks,
)
from ai_trainer.domain.knowledge import KnowledgeSource, SourceLanguage
from ai_trainer.domain.knowledge_document import embedding_input
from ai_trainer.domain.knowledge_ingestion import IngestFailure

# How many chunks the finished source holds, why it failed, or that its claim was lost.
type CatalogueOutcome = int | IngestFailure | Literal["gone"]


class CatalogueIngestion:
    def __init__(
        self,
        *,
        repository: CatalogueChunksRepositoryPort,
        embeddings: EmbeddingGatewayPort,
        clock: ClockPort,
        sleep: Callable[[float], Awaitable[None]],
        batch_size: int,
        max_attempts: int,
        model: str,
    ) -> None:
        self._repository = repository
        self._embeddings = embeddings
        self._clock = clock
        self._sleep = sleep
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        # The configured `EMBEDDING_MODEL`, which search filters on (the #424 amendment): a
        # chunk another model embedded is stale.
        self._model = model

    async def ingest(
        self, source: KnowledgeSource, claim_id: UUID, lost: asyncio.Event
    ) -> CatalogueOutcome:
        acting_user = source.uploaded_by_user_id
        if acting_user is None:
            return IngestFailure.NO_ACTING_ADMIN
        texts = await self._repository.texts(SourceLanguage(source.language))
        stale = stale_chunks(
            texts, await self._repository.chunk_states(source.id), model=self._model
        )
        for start in range(0, len(stale), self._batch_size):
            if lost.is_set():
                return "gone"
            batch = stale[start : start + self._batch_size]
            outcome = await embed_batch(
                self._embeddings,
                [embedding_input(source.title, item.chunk) for item in batch],
                acting_user=acting_user,
                sleep=self._sleep,
                max_attempts=self._max_attempts,
            )
            if isinstance(outcome, IngestFailure):
                return outcome
            written = await self._repository.write_batch(
                source.id,
                claim_id=claim_id,
                chunks=[
                    EmbeddedCatalogueChunk(item=item, embedding=vector)
                    for item, vector in zip(batch, outcome.vectors, strict=True)
                ],
                model=self._model,
                at=self._clock.now(),
            )
            if not written:
                return "gone"
        if lost.is_set():
            return "gone"
        keep = kept_exercises(texts)
        finished = await self._repository.finish(
            source.id,
            claim_id=claim_id,
            keep=keep,
            model=self._model,
            ingested_at=self._clock.now(),
        )
        return len(keep) if finished else "gone"
