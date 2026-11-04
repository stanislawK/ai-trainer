"""What `search_knowledge` asks and returns (ADR-0033 *Retrieval*, ADR-0036, #424): passages
ready to cite, each with its source's title, author, tier and locator (PRD B8), fused from a
vector ranking and a full-text ranking by reciprocal rank fusion."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import Locator

# Cormack, Clarke and Büttcher's constant, which pgvector's `rrf.py` uses too
# (docs/research/hybrid-search.md finding 1).
RRF_K = 60
DEFAULT_HITS = 5
MAX_HITS = 10


class KnowledgeHit(BaseModel):
    """One passage and what a reply needs to cite it. No score and no internal field."""

    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    text: str
    heading_path: tuple[str, ...]
    language: SourceLanguage
    source_id: UUID
    title: str
    author: str | None
    evidence_tier: EvidenceTier
    source_type: SourceType
    source_url: str | None
    attribution: str | None
    locator: Locator
    exercise_id: UUID | None


class KnowledgeSearchPage(BaseModel):
    """The best passages first. `truncated` is true when more passages matched than were
    asked for."""

    model_config = ConfigDict(frozen=True)

    hits: tuple[KnowledgeHit, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class KnowledgeSearchQuery:
    text: str
    sport_id: str | None = None
    topics: tuple[str, ...] = ()
    exercise_id: UUID | None = None
    limit: int = DEFAULT_HITS


@dataclass(frozen=True, slots=True)
class KnowledgeFilters:
    """What both legs filter on. The embedding model is always one of them (ADR-0011 invariant
    4); language never filters the vector leg (hybrid-search.md finding 5)."""

    embedding_model: str
    sport_id: str | None = None
    topics: tuple[str, ...] = ()
    exercise_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeCandidates:
    """Each leg's passages, best first."""

    by_meaning: tuple[KnowledgeHit, ...]
    by_words: tuple[KnowledgeHit, ...]


type KnowledgeSearchFailure = Literal[
    "blank_query", "unknown_sport", "no_match", "embedding_unavailable"
]


@dataclass(frozen=True, slots=True)
class KnowledgeSearchFailed:
    reason: KnowledgeSearchFailure
    known_sports: tuple[str, ...] = field(default=())


def rrf_fuse(rankings: Sequence[Sequence[UUID]], *, k: int = RRF_K) -> list[UUID]:
    """Every ID in any ranking, by the sum over rankings of `1 / (k + rank)`, rank from 1. Ties
    keep the order in which an ID was first seen, the first ranking's before the second's."""
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: -scores[item])
