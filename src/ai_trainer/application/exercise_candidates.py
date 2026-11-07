"""The exercise shortlist with the catalogue's search as a third input (ADR-0031, ticket #429).

A decorator over `ExerciseNamesPort`: the trigram and full-text list is today's, the search by
meaning is the new one, and the two exercise rankings are fused by reciprocal rank (k = 60,
`hybrid-search.md` finding 7). History is read before any shortlist, so a name the athlete already
settled never reaches this class. A slow, failed or empty search leaves today's list."""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from uuid import UUID

from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.ports.exercises import (
    ExerciseCandidatesPort,
    ExerciseNamesPort,
    ExerciseSearchPort,
)
from ai_trainer.domain.embeddings import QueryTask
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseLabel
from ai_trainer.domain.knowledge_search import (
    MAX_HITS,
    KnowledgeSearchFailed,
    KnowledgeSearchQuery,
    rrf_fuse,
)
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID

logger = logging.getLogger(__name__)

# How deep the trigram list is read before fusion, as the search's own legs are (`Settings`
# `knowledge_search_depth`). Fusion needs ranks past the cap to tell a double hit from a single.
TRIGRAM_DEPTH = 20
# How many athletes' latest searches the shortlist keeps. IDs only, no text but the typed name.
REMEMBERED_SEARCHES = 256


class SearchedExerciseNames:
    """Implements `ExerciseNamesPort`; everything but `shortlist` is the inner port's."""

    def __init__(
        self,
        inner: ExerciseNamesPort,
        search: ExerciseSearchPort,
        candidates: ExerciseCandidatesPort,
        *,
        timeout_seconds: float,
        remembered: int = REMEMBERED_SEARCHES,
    ) -> None:
        self._inner = inner
        self._search = search
        self._candidates = candidates
        self._timeout = timeout_seconds
        self._remembered = remembered
        # The latest successful searches by athlete and typed name. A name Jev wasn't sure of is
        # shortlisted twice, once to pick and once for the card, and one embedding serves both.
        self._recent: OrderedDict[tuple[UUID, str], Sequence[UUID]] = OrderedDict()

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> Mapping[str, UUID]:
        return await self._inner.match_user_aliases(
            user_id, sport_id=sport_id, names=names, near=near
        )

    async def labels(
        self, user_id: UUID, *, named: Mapping[str, UUID], near: float
    ) -> Mapping[str, ExerciseLabel]:
        return await self._inner.labels(user_id, named=named, near=near)

    async def shortlist(
        self,
        user_id: UUID,
        *,
        sport_id: str,
        name: str,
        measure: str,
        floor: float,
        limit: int,
    ) -> Sequence[ExerciseCandidate]:
        # The search starts first, so its embedding call overlaps the database read.
        searching = asyncio.ensure_future(self._similar(user_id, name))
        try:
            trigram = await self._inner.shortlist(
                user_id,
                sport_id=sport_id,
                name=name,
                measure=measure,
                floor=floor,
                limit=max(limit, TRIGRAM_DEPTH),
            )
        except BaseException:
            searching.cancel()
            raise
        found = await searching
        if not found:
            return list(trigram)[:limit]
        by_id = {candidate.id: candidate for candidate in trigram}
        fused = rrf_fuse([[candidate.id for candidate in trigram], found])[:limit]
        missing = [exercise_id for exercise_id in fused if exercise_id not in by_id]
        if missing:
            by_id |= {c.id: c for c in await self._candidates.candidates(user_id, missing)}
        # An exercise the athlete can't see has no candidate and is dropped.
        return [by_id[exercise_id] for exercise_id in fused if exercise_id in by_id]

    async def _similar(self, user_id: UUID, name: str) -> Sequence[UUID]:
        """The search's IDs, or none when it is slow or fails: the log still drafts. Only a
        search that answered is remembered."""
        key = (user_id, name)
        if key in self._recent:
            self._recent.move_to_end(key)
            return self._recent[key]
        try:
            async with asyncio.timeout(self._timeout):
                found = await self._search.similar(user_id, name)
        except Exception:  # any failure of the extra input falls back to today's list
            logger.warning("exercise search unavailable, using the trigram shortlist")
            return ()
        self._recent[key] = found
        while len(self._recent) > self._remembered:
            self._recent.popitem(last=False)
        return found


class ExerciseSearchError(Exception):
    """The search found nothing to use: its failure reason."""


class KnowledgeExerciseSearch:
    """Implements `ExerciseSearchPort` over `search_knowledge`: the gym's catalogue chunks, each
    exercise once at its best passage. The embedding call is the athlete's, as in any search."""

    def __init__(self, knowledge: KnowledgeSearch) -> None:
        self._knowledge = knowledge

    async def similar(self, user_id: UUID, name: str) -> Sequence[UUID]:
        found = await self._knowledge.search(
            user_id,
            KnowledgeSearchQuery(
                text=name, sport_id=GYM_SPORT_ID, limit=MAX_HITS, task=QueryTask.EXERCISE
            ),
        )
        if isinstance(found, KnowledgeSearchFailed):
            raise ExerciseSearchError(found.reason)
        return list(dict.fromkeys(hit.exercise_id for hit in found.hits if hit.exercise_id))
