"""`search_knowledge`'s use case (ADR-0033 *Retrieval*, ADR-0036, #424): embed the question,
read a vector leg and a full-text leg under the same filters, fuse them by reciprocal rank
fusion at k = 60, and cut at `limit`.

When the embedding call fails, the search fails: a full-text-only answer would silently drop the
passages in the other language, which only the vector leg finds (hybrid-search.md finding 5)."""

from uuid import UUID

from ai_trainer.application.ports.embeddings import QueryEmbeddingPort
from ai_trainer.application.ports.knowledge import KnowledgeSearchPort
from ai_trainer.domain.embeddings import Embedded
from ai_trainer.domain.knowledge_search import (
    KnowledgeFilters,
    KnowledgeSearchFailed,
    KnowledgeSearchPage,
    KnowledgeSearchQuery,
    rrf_fuse,
)
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError


class KnowledgeSearch:
    def __init__(
        self,
        *,
        embeddings: QueryEmbeddingPort,
        repository: KnowledgeSearchPort,
        sports: SportRegistry,
        embedding_model: str,
        depth: int,
    ) -> None:
        self._embeddings = embeddings
        self._repository = repository
        self._sports = sports
        self._embedding_model = embedding_model
        self._depth = depth

    async def search(
        self, user_id: UUID, query: KnowledgeSearchQuery
    ) -> KnowledgeSearchPage | KnowledgeSearchFailed:
        """The best passages for `query`. `user_id` is only the embedding call's acting user
        (ADR-0018): the chunks are nobody's."""
        text = query.text.strip()
        if not text:
            return KnowledgeSearchFailed(reason="blank_query")
        if query.sport_id is not None:
            try:
                self._sports.get(query.sport_id)
            except UnknownSportError:
                return KnowledgeSearchFailed(
                    reason="unknown_sport",
                    known_sports=tuple(plugin.id for plugin in self._sports.all()),
                )
        embedded = await self._embeddings.embed_query(text, acting_user_id=user_id, task=query.task)
        if not isinstance(embedded, Embedded) or len(embedded.vectors) != 1:
            return KnowledgeSearchFailed(reason="embedding_unavailable")
        candidates = await self._repository.candidates(
            vector=embedded.vectors[0],
            query=text,
            filters=KnowledgeFilters(
                embedding_model=self._embedding_model,
                sport_id=query.sport_id,
                topics=tuple(t for topic in query.topics if (t := topic.strip().lower())),
                exercise_id=query.exercise_id,
            ),
            depth=self._depth,
        )
        hits = {hit.chunk_id: hit for hit in (*candidates.by_meaning, *candidates.by_words)}
        if not hits:
            return KnowledgeSearchFailed(reason="no_match")
        fused = rrf_fuse(
            [
                [hit.chunk_id for hit in candidates.by_meaning],
                [hit.chunk_id for hit in candidates.by_words],
            ]
        )
        return KnowledgeSearchPage(
            hits=tuple(hits[chunk_id] for chunk_id in fused[: query.limit]),
            truncated=len(fused) > query.limit,
        )
