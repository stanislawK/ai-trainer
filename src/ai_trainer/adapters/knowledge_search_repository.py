"""`search_knowledge`'s two legs on PostgreSQL and pgvector (ADR-0033 *Retrieval*, #424).

- **Vector leg:** cosine distance on the HNSW index, with `hnsw.iterative_scan = relaxed_order`
  set for this transaction only (`SET LOCAL`), so a filter applied after the index scan still
  fills the list, and a pooled connection never carries the setting on. Relaxed order may come
  back slightly out of order, so the rows are sorted by distance again here (pgvector README,
  *Iterative Index Scans*). Never filtered by language: an English question should find a
  Polish passage (docs/research/hybrid-search.md finding 5).
- **Full-text leg:** one list, the question parsed by `websearch_to_tsquery` in `english` for
  English chunks and in `simple` for Polish ones, the configurations their `tsvector` was built
  with (ADR-0033), ranked by `ts_rank_cd`.

Both legs filter on the embedding model (ADR-0011 invariant 4) and the caller's sport, topics
and exercise.
"""

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    and_,
    case,
    cast,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Text

from ai_trainer.adapters.orm import KnowledgeChunkOrm, KnowledgeSourceOrm
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_search import KnowledgeCandidates, KnowledgeFilters, KnowledgeHit

_C = KnowledgeChunkOrm
_S = KnowledgeSourceOrm


def _filters(filters: KnowledgeFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [_C.embedding_model == filters.embedding_model]
    if filters.sport_id is not None:
        conditions.append(_C.sports.any_() == filters.sport_id)
    if filters.topics:
        conditions.append(_C.topics.overlap(cast(list(filters.topics), ARRAY(Text))))
    if filters.exercise_id is not None:
        conditions.append(_C.exercise_id == filters.exercise_id)
    return conditions


def _hits(*extra: ColumnElement[Any]) -> Select[Any]:
    return select(
        _C.id,
        _C.text,
        _C.heading_path,
        _C.language,
        _C.locator,
        _C.exercise_id,
        _S.id.label("source_id"),
        _S.title,
        _S.author,
        _S.evidence_tier,
        _S.source_type,
        _S.source_url,
        _S.attribution,
        *extra,
    ).join(_S, _S.id == _C.source_id)


def _to_hit(row: Row[Any]) -> KnowledgeHit:
    return KnowledgeHit.model_validate(
        {
            "chunk_id": row.id,
            "text": row.text,
            "heading_path": tuple(row.heading_path),
            "language": SourceLanguage(row.language),
            "source_id": row.source_id,
            "title": row.title,
            "author": row.author,
            "evidence_tier": EvidenceTier(row.evidence_tier),
            "source_type": SourceType(row.source_type),
            "source_url": row.source_url,
            "attribution": row.attribution,
            "locator": row.locator,
            "exercise_id": row.exercise_id,
        }
    )


def _tsquery(config: str, query: str) -> ColumnElement[Any]:
    return func.websearch_to_tsquery(cast(literal(config), REGCONFIG), query)


class SqlAlchemyKnowledgeSearch:
    """Implements `KnowledgeSearchPort`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def candidates(
        self,
        *,
        vector: Sequence[float],
        query: str,
        filters: KnowledgeFilters,
        depth: int,
    ) -> KnowledgeCandidates:
        conditions = _filters(filters)
        distance = _C.embedding.cosine_distance(list(vector)).label("distance")
        # The distance alone, so the plan is the HNSW index scan with no sort on top; ties are
        # broken by ID in the re-sort below.
        by_meaning = _hits(distance).where(*conditions).order_by(distance).limit(depth)
        english, simple = _tsquery("english", query), _tsquery("simple", query)
        is_english = _C.language == SourceLanguage.EN.value
        rank = case(
            (is_english, func.ts_rank_cd(_C.search_vector, english)),
            else_=func.ts_rank_cd(_C.search_vector, simple),
        ).label("rank")
        by_words = (
            _hits(rank)
            .where(
                *conditions,
                or_(
                    and_(is_english, _C.search_vector.op("@@")(english)),
                    and_(
                        _C.language == SourceLanguage.PL.value,
                        _C.search_vector.op("@@")(simple),
                    ),
                ),
            )
            .order_by(rank.desc(), _C.id)
            .limit(depth)
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            meaning_rows = sorted((await session.execute(by_meaning)).all(), key=_by_distance)
            word_rows = (await session.execute(by_words)).all()
        return KnowledgeCandidates(
            by_meaning=tuple(_to_hit(row) for row in meaning_rows),
            by_words=tuple(_to_hit(row) for row in word_rows),
        )


def _by_distance(row: Row[Any]) -> tuple[float, str]:
    return (float(row.distance), str(row.id))
