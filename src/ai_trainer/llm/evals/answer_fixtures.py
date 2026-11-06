"""The fixtures an answer template's smoke case runs on (ADR-0034, #421, #428): what the athlete
logged and the knowledge base's passages, behind the ports the MCP tools read. A case has one
athlete and no real identity."""

from collections.abc import Mapping, Sequence
from datetime import date
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel

from ai_trainer.domain.embeddings import Embedded, EmbeddingOutcome
from ai_trainer.domain.exercises import (
    CatalogueExercise,
    CatalogueWrite,
    Exercise,
    ExerciseAlias,
    ExerciseCandidate,
    ExerciseDetail,
    ExerciseEntry,
    ExerciseLabel,
    ExerciseMeasure,
    LoggedName,
    LoggedSet,
)
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import Locator, SectionLocator
from ai_trainer.domain.knowledge_search import KnowledgeCandidates, KnowledgeFilters, KnowledgeHit
from ai_trainer.domain.training import SessionSummary

# The fixture's own: no chunk is filtered by it, and no embedding call is made.
EMBEDDING_MODEL = "smoke"


class SetFixture(BaseModel):
    reps: int | None = None
    load_kg: float | None = None
    load_original: str | None = None
    duration_s: int | None = None
    band_label: str | None = None


class LoggedFixture(BaseModel):
    """One logged exercise. `exercise` is the name it is linked to; none leaves it typed-only."""

    day: date
    typed: str
    exercise: str | None = None
    per_side: bool = False
    implements: int | None = None
    sets: list[SetFixture]


class PassageFixture(BaseModel):
    """One passage of a case's knowledge base."""

    title: str
    author: str | None = None
    evidence_tier: EvidenceTier
    source_type: SourceType
    locator: Locator = SectionLocator()
    language: SourceLanguage = SourceLanguage.EN
    text: str


def fixture_id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, _norm(name))


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


def _fits(one: str, other: str) -> bool:
    """Stands in for the database's word similarity: one name inside the other."""
    return _norm(one) in _norm(other) or _norm(other) in _norm(one)


class CaseHistory:
    """Implements the ports the tools read, over one case's fixture. The case has one athlete."""

    def __init__(
        self, sport_id: str, logged: Sequence[LoggedFixture], catalogue: Sequence[str]
    ) -> None:
        self._sport_id = sport_id
        self._logged = logged
        self._catalogue = catalogue

    async def list_sessions(
        self,
        user_id: UUID,
        *,
        sport_id: str | None,
        since: date | None,
        until: date | None,
        limit: int,
    ) -> Sequence[SessionSummary]:
        days = sorted(
            {
                entry.day
                for entry in self._logged
                if (since is None or entry.day >= since) and (until is None or entry.day <= until)
            },
            reverse=True,
        )
        if sport_id not in (None, self._sport_id):
            return []
        return [
            SessionSummary(id=fixture_id(str(day)), sport_id=self._sport_id, occurred_on=day)
            for day in days[:limit]
        ]

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> Mapping[str, UUID]:
        return {
            name: fixture_id(entry.exercise)
            for name in names
            for entry in self._logged
            if entry.exercise is not None and _norm(name) == _norm(entry.typed)
        }

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
        return [
            ExerciseCandidate(id=fixture_id(listed), name=listed)
            for listed in self._catalogue
            if _fits(name, listed)
        ][:limit]

    async def labels(
        self, user_id: UUID, *, named: Mapping[str, UUID], near: float
    ) -> Mapping[str, ExerciseLabel]:
        return {}  # pragma: no cover -- no tool reads a draft card's labels

    async def describe(
        self, user_id: UUID, exercise_ids: Sequence[UUID], *, lang: str
    ) -> Sequence[ExerciseDetail]:
        names = {fixture_id(name): name for name in self._exercise_names()}
        return [
            ExerciseDetail(id=exercise_id, name=names[exercise_id])
            for exercise_id in exercise_ids
            if exercise_id in names
        ]

    async def get(self, user_id: UUID, exercise_id: UUID) -> Exercise | None:
        for name in self._exercise_names():
            if fixture_id(name) == exercise_id:
                return Exercise(
                    id=exercise_id,
                    sport_id=self._sport_id,
                    owner_user_id=None,
                    name=name,
                    default_measure=ExerciseMeasure.LOAD_REPS,
                )
        return None

    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        return self._read(
            [
                e
                for e in self._logged
                if e.exercise is not None and fixture_id(e.exercise) == exercise_id
            ],
            limit,
        )

    async def typed_entries(
        self, user_id: UUID, typed_name: str, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        return self._read(
            [
                entry
                for entry in self._logged
                if entry.exercise is None and _norm(entry.typed) == _norm(typed_name)
            ],
            limit,
        )

    async def logged_names(
        self, user_id: UUID, name: str, *, near: float, limit: int
    ) -> Sequence[LoggedName]:
        counts: dict[tuple[UUID | None, str], int] = {}
        for entry in self._logged:
            if not (_fits(name, entry.typed) or (entry.exercise and _fits(name, entry.exercise))):
                continue
            key = (
                (fixture_id(entry.exercise), entry.exercise)
                if entry.exercise is not None
                else (None, _norm(entry.typed))
            )
            counts[key] = counts.get(key, 0) + 1
        return [
            LoggedName(exercise_id=exercise_id, name=logged, entries=count)
            for (exercise_id, logged), count in counts.items()
        ][:limit]

    def _exercise_names(self) -> list[str]:
        linked = [entry.exercise for entry in self._logged if entry.exercise is not None]
        return [*linked, *self._catalogue]

    def _read(self, found: Sequence[LoggedFixture], limit: int) -> list[ExerciseEntry]:
        latest_first = sorted(found, key=lambda entry: entry.day, reverse=True)
        return [
            ExerciseEntry(
                session_id=fixture_id(str(entry.day)),
                occurred_on=entry.day,
                name_as_typed=entry.typed,
                measure=ExerciseMeasure.LOAD_REPS,
                per_side=entry.per_side,
                implements=entry.implements,
                sets=tuple(
                    LoggedSet(set_no=number, **logged_set.model_dump())
                    for number, logged_set in enumerate(entry.sets, start=1)
                ),
            )
            for entry in latest_first[:limit]
        ]

    async def upsert_catalogue(self, exercises: Sequence[CatalogueExercise]) -> CatalogueWrite:
        raise NotImplementedError  # pragma: no cover -- no tool writes the catalogue

    async def create_custom(
        self, user_id: UUID, *, sport_id: str, name: str, default_measure: ExerciseMeasure
    ) -> Exercise:
        raise NotImplementedError  # pragma: no cover -- no tool creates an exercise

    async def add_user_alias(
        self, user_id: UUID, exercise_id: UUID, alias: str
    ) -> ExerciseAlias | None:
        raise NotImplementedError  # pragma: no cover -- no tool writes an alias

    async def user_aliases(self, user_id: UUID) -> Sequence[ExerciseAlias]:
        raise NotImplementedError  # pragma: no cover -- no tool reads every alias


class CaseKnowledge:
    """Implements the search's ports over one case's passages: every passage comes back by
    meaning, in the case's order, under any filter."""

    def __init__(self, passages: Sequence[PassageFixture]) -> None:
        self._hits = tuple(
            KnowledgeHit(
                chunk_id=fixture_id(f"{passage.title}/{number}"),
                text=passage.text,
                heading_path=(),
                language=passage.language,
                source_id=fixture_id(passage.title),
                title=passage.title,
                author=passage.author,
                evidence_tier=passage.evidence_tier,
                source_type=passage.source_type,
                source_url=None,
                attribution=None,
                locator=passage.locator,
                exercise_id=None,
            )
            for number, passage in enumerate(passages)
        )

    async def embed_query(self, text: str, *, acting_user_id: UUID) -> EmbeddingOutcome:
        return Embedded(vectors=((1.0,),), model=EMBEDDING_MODEL)

    async def candidates(
        self, *, vector: Sequence[float], query: str, filters: KnowledgeFilters, depth: int
    ) -> KnowledgeCandidates:
        return KnowledgeCandidates(by_meaning=self._hits[:depth], by_words=())
