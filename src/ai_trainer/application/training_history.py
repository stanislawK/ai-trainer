"""Reading the athlete's saved sessions back (ADR-0010, ticket #378): the use case behind the
`query_sessions` MCP tool. A query that can't be answered comes back as a
`SessionQueryFailed` value, never a raise, so a tool can turn it into a recovery hint."""

import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from ai_trainer.application.ports.exercises import ExerciseHistoryPort, ExerciseRepositoryPort
from ai_trainer.application.ports.training import SessionHistoryPort
from ai_trainer.domain.exercises import ExerciseEntry, ExerciseMeasure, LoggedSet
from ai_trainer.domain.sports.gym.pace import Split, clock_text, pace_per_split, split_of
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.training import SessionSummary

DEFAULT_SESSIONS = 10
MAX_SESSIONS = 50


class SessionQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    sport_id: str | None = None
    since: date | None = None
    until: date | None = None
    limit: int = Field(default=DEFAULT_SESSIONS, ge=1, le=MAX_SESSIONS)


class SessionPage(BaseModel):
    """The sessions found, the latest day first; `truncated` when more matched than `limit`."""

    model_config = ConfigDict(frozen=True)

    sessions: list[SessionSummary]
    truncated: bool


type SessionQueryFailure = Literal["unknown_sport", "since_after_until", "no_sessions"]


class SessionQueryFailed(BaseModel):
    """Why a query found nothing to return; `known_sports` lists the sport IDs to pick from
    when the sport was unknown."""

    model_config = ConfigDict(frozen=True)

    reason: SessionQueryFailure
    known_sports: tuple[str, ...] = ()


class TrainingHistory:
    def __init__(self, sessions: SessionHistoryPort, sports: SportRegistry) -> None:
        self._sessions = sessions
        self._sports = sports

    async def query(self, user_id: UUID, query: SessionQuery) -> SessionPage | SessionQueryFailed:
        """The user's own sessions matching `query`. An unknown sport, a range that ends before
        it starts, or no match at all is a `SessionQueryFailed`."""
        if query.sport_id is not None:
            try:
                self._sports.get(query.sport_id)
            except UnknownSportError:
                return SessionQueryFailed(
                    reason="unknown_sport",
                    known_sports=tuple(plugin.id for plugin in self._sports.all()),
                )
        if query.since is not None and query.until is not None and query.since > query.until:
            return SessionQueryFailed(reason="since_after_until")
        # One row past the limit tells a cut page from an exact one.
        found = await self._sessions.list_sessions(
            user_id,
            sport_id=query.sport_id,
            since=query.since,
            until=query.until,
            limit=query.limit + 1,
        )
        if not found:
            return SessionQueryFailed(reason="no_sessions")
        return SessionPage(sessions=list(found[: query.limit]), truncated=len(found) > query.limit)


DEFAULT_ENTRIES = 10
MAX_ENTRIES = 50


class ExerciseRef(BaseModel):
    """The exercise a history is of, as a tool shows it: no owner, which is the athlete's own ID
    for a custom exercise."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    default_measure: ExerciseMeasure


_SPLIT_TEXT: Mapping[Split, str] = {"500m": "/500 m", "km": "/km"}


class _WithoutNulls(BaseModel):
    """A model that dumps without its `None` fields: a gym answer resends every tool result on
    each request of its tool loop, so a null per missing measure is tokens for nothing."""

    # No return annotation on purpose: pydantic reads one as the serialization schema, which
    # would turn each model's tool output schema into a bare object (#662 code review).
    @model_serializer(mode="wrap")
    def _drop_nulls(  # type: ignore[no-untyped-def]  # an annotation would erase the JSON schema
        self, handler: SerializerFunctionWrapHandler
    ):
        dumped: dict[str, Any] = handler(self)
        return {key: value for key, value in dumped.items() if value is not None}


_KG_TEXT = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*(?:kg|kgs)?\s*", re.IGNORECASE)


def _restates_kg(logged: LoggedSet) -> bool:
    """True when `load_original` is only the kilograms in `load_kg` ("45kg", "62,5 kg")."""
    if logged.load_original is None or logged.load_kg is None:
        return False
    matched = _KG_TEXT.fullmatch(logged.load_original)
    return matched is not None and float(matched[1].replace(",", ".")) == logged.load_kg


class HistorySet(_WithoutNulls, LoggedSet):
    """A logged set as a tool shows it: `pace` is worked out in code from a distance and a time
    both above 0, as "2:05 /500 m" or "5:36 /km", and is `None` otherwise, never estimated or
    stored (PRD 0013 B42, ADR-0032 invariant 6)."""

    pace: str | None = None

    @model_serializer(mode="wrap")
    def _drop_nulls(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Also leaves the load typed out when it only restates `load_kg` ("45kg"), and keeps it
        otherwise ("100 lb"), so the answer sees each load once."""
        dumped: dict[str, Any] = super()._drop_nulls(handler)
        if _restates_kg(self):
            dumped.pop("load_original", None)
        return dumped


class HistoryEntry(_WithoutNulls, ExerciseEntry):
    """A logged entry as a tool shows it, its sets with their pace and no session ID, which no
    tool takes. The ID stays on the model for the progress chart's own reads (#517)."""

    session_id: UUID = Field(exclude=True)
    sets: tuple[HistorySet, ...]


def _shown(entry: ExerciseEntry, exercise: ExerciseRef | None) -> HistoryEntry:
    split = split_of(exercise.name if exercise else None, entry.name_as_typed)
    return HistoryEntry(
        **entry.model_dump(exclude={"sets"}),
        sets=tuple(
            HistorySet(**logged.model_dump(), pace=_pace_text(logged, split))
            for logged in entry.sets
        ),
    )


def _pace_text(logged: LoggedSet, split: Split) -> str | None:
    seconds = pace_per_split(logged, split)
    return None if seconds is None else f"{clock_text(seconds)} {_SPLIT_TEXT[split]}"


class ExerciseHistoryPage(_WithoutNulls):
    """The exercise and the athlete's entries of it, the latest day first; `truncated` when more
    exist than `limit`. Typed-only entries have no `exercise` (ADR-0031)."""

    model_config = ConfigDict(frozen=True)

    exercise: ExerciseRef | None = None
    entries: list[HistoryEntry]
    truncated: bool


class ExerciseHistoryFailed(BaseModel):
    """`not_found` covers an unknown ID and another athlete's alike; `no_entries` an exercise the
    athlete can see but has never logged, or a name with no typed-only entries; `bad_selector` a
    call naming the exercise both ways, or neither."""

    model_config = ConfigDict(frozen=True)

    reason: Literal["not_found", "no_entries", "bad_selector"]


class ExerciseTrainingHistory:
    def __init__(self, exercises: ExerciseRepositoryPort, history: ExerciseHistoryPort) -> None:
        self._exercises = exercises
        self._history = history

    async def entries(
        self, user_id: UUID, exercise_id: UUID, limit: int = DEFAULT_ENTRIES
    ) -> ExerciseHistoryPage | ExerciseHistoryFailed:
        """The user's own entries of one exercise they can see, read from the typed rows."""
        exercise = await self._exercises.get(user_id, exercise_id)
        if exercise is None:
            return ExerciseHistoryFailed(reason="not_found")
        limit = _bounded(limit)
        # One row past the limit tells a cut page from an exact one.
        found = await self._history.entries(user_id, exercise_id, limit=limit + 1)
        return _page(
            ExerciseRef(
                id=exercise.id, name=exercise.name, default_measure=exercise.default_measure
            ),
            found,
            limit,
        )

    async def typed_entries(
        self, user_id: UUID, typed_name: str, limit: int = DEFAULT_ENTRIES
    ) -> ExerciseHistoryPage | ExerciseHistoryFailed:
        """The user's own typed-only entries of a name, which no exercise is linked to yet."""
        limit = _bounded(limit)
        found = await self._history.typed_entries(user_id, typed_name, limit=limit + 1)
        return _page(None, found, limit)


def _bounded(limit: int) -> int:
    return max(1, min(limit, MAX_ENTRIES))


def _page(
    exercise: ExerciseRef | None, found: Sequence[ExerciseEntry], limit: int
) -> ExerciseHistoryPage | ExerciseHistoryFailed:
    if not found:
        return ExerciseHistoryFailed(reason="no_entries")
    return ExerciseHistoryPage(
        exercise=exercise,
        entries=[_shown(entry, exercise) for entry in found[:limit]],
        truncated=len(found) > limit,
    )
