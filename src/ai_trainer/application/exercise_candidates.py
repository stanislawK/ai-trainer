"""The exercise shortlist with the catalogue's search as a third input (ADR-0031, ticket #429).

A decorator over `ExerciseNamesPort`: the trigram and full-text list is today's, the search by
meaning is the new one, and the two exercise rankings are fused by reciprocal rank (k = 60,
`hybrid-search.md` finding 7). History is read before any shortlist, so a name the athlete already
settled never reaches this class. A slow, failed or empty search leaves today's list."""

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.exercises import (
    ExerciseCandidatesPort,
    ExerciseNamesPort,
    ExerciseSearchPort,
)
from ai_trainer.domain.embeddings import QueryTask
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseLabel
from ai_trainer.domain.knowledge_search import (
    KnowledgeSearchFailed,
    KnowledgeSearchQuery,
    rrf_fuse,
)
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID

logger = logging.getLogger(__name__)

# How deep the trigram list is read before fusion, as the search's own legs are (`Settings`
# `knowledge_search_depth`). Fusion needs ranks past the cap to tell a double hit from a single.
TRIGRAM_DEPTH = 20
# How many exercises of the fused ranking are read before the cut, so the entry's own measure can
# put its exercises ahead of another measure's wherever they ranked.
FUSED_DEPTH = 2 * TRIGRAM_DEPTH
# How many athletes' latest searches the shortlist keeps. IDs only, no text but the typed name.
REMEMBERED_SEARCHES = 256
# A name Jev wasn't sure of is shortlisted twice, once to pick and once for the card, seconds
# apart, and one embedding serves both. An answer is kept for a while, so a catalogue re-ingest is
# seen within minutes; a failure only for the turn, so it can't keep a name off the search.
ANSWER_MEMORY = timedelta(minutes=10)
FAILURE_MEMORY = timedelta(seconds=30)


class SearchedExerciseNames:
    """Implements `ExerciseNamesPort`; everything but `shortlist` is the inner port's."""

    def __init__(
        self,
        inner: ExerciseNamesPort,
        search: ExerciseSearchPort,
        candidates: ExerciseCandidatesPort,
        *,
        timeout_seconds: float,
        clock: ClockPort,
        remembered: int = REMEMBERED_SEARCHES,
    ) -> None:
        self._inner = inner
        self._search = search
        self._candidates = candidates
        self._timeout = timeout_seconds
        self._clock = clock
        self._remembered = remembered
        # The latest searches by athlete and typed name, each with the time it stops being served.
        self._recent: OrderedDict[tuple[UUID, str], tuple[datetime, Sequence[UUID]]] = OrderedDict()

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
            with contextlib.suppress(asyncio.CancelledError):
                await searching
            raise
        found = await searching
        if not found:
            return list(trigram)[:limit]
        fused = rrf_fuse([[candidate.id for candidate in trigram], found])[:FUSED_DEPTH]
        try:
            # Read in one go with the entry's own measure first, as the trigram list orders it, so
            # another measure's exercise (a plank for a sets-and-reps entry) can't take a slot from
            # one that fits. An exercise the athlete can't see comes back missing and is dropped.
            read = await self._candidates.candidates(user_id, fused, measure=measure)
        except Exception:  # the extra input never fails the log: today's list stands
            logger.warning("exercise search candidates unreadable, using the trigram shortlist")
            return list(trigram)[:limit]
        return list(read)[:limit]

    async def _similar(self, user_id: UUID, name: str) -> Sequence[UUID]:
        """The search's IDs, or none when it is slow or fails: the log still drafts. A failure is
        remembered for the turn, so the card shows the shortlist Jev was shown and does not wait
        out a second timeout."""
        key = (user_id, name)
        now = self._clock.now()
        remembered = self._recent.get(key)
        if remembered is not None:
            expires_at, found = remembered
            if now < expires_at:
                self._recent.move_to_end(key)
                return found
            del self._recent[key]
        try:
            async with asyncio.timeout(self._timeout):
                found = await self._search.similar(user_id, name)
            memory = ANSWER_MEMORY
        except Exception as error:  # any failure of the extra input falls back to today's list
            logger.warning(
                "exercise search unavailable (%s), using the trigram shortlist", _reason(error)
            )
            found = ()
            memory = FAILURE_MEMORY
        self._recent[key] = (now + memory, found)
        while len(self._recent) > self._remembered:
            self._recent.popitem(last=False)
        return found


class ExerciseSearchError(Exception):
    """The search could not be used. Its message is the failure reason (`no_match`,
    `embedding_unavailable`, `unknown_sport`, `no_exercise`), safe to log: it holds no typed
    text."""


def _reason(error: Exception) -> str:
    """What to log of a failed search: the reason of one we raised, else the kind of error. Never
    the error's own text, which could carry the typed name."""
    return str(error) if isinstance(error, ExerciseSearchError) else type(error).__name__


class KnowledgeExerciseSearch:
    """Implements `ExerciseSearchPort` over `search_knowledge`: the gym's catalogue chunks, each
    exercise once at its best passage. The embedding call is the athlete's, as in any search.

    `chunks` is how many fused passages to read before grouping them by exercise: every chunk of
    both legs (twice `knowledge_search_depth`), since an exercise has an English and a Polish one
    and a cut at the tool's 10 would leave five exercises or fewer."""

    def __init__(self, knowledge: KnowledgeSearch, *, chunks: int) -> None:
        self._knowledge = knowledge
        self._chunks = chunks

    async def similar(self, user_id: UUID, name: str) -> Sequence[UUID]:
        found = await self._knowledge.search(
            user_id,
            KnowledgeSearchQuery(
                text=name,
                sport_id=GYM_SPORT_ID,
                limit=self._chunks,
                task=QueryTask.EXERCISE,
                exercises_only=True,
            ),
        )
        if isinstance(found, KnowledgeSearchFailed):
            raise ExerciseSearchError(found.reason)
        exercises = list(dict.fromkeys(hit.exercise_id for hit in found.hits if hit.exercise_id))
        if not exercises:
            raise ExerciseSearchError("no_exercise")
        return exercises
