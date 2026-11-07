"""`find_exercise` (B37, ADR-0031, ADR-0010, ticket #401): the athlete's own answer to "which
exercise is this name", read-only and in code, with no model call. History first, as naming does:
a name the athlete already settled comes back alone. Otherwise a shortlist of at most
`SHORTLIST_MAX` catalogue or custom exercises. A name that fits nothing is an
`ExerciseLookupFailed` value, never a raise, so a tool can turn it into a recovery hint."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.application.exercise_resolution import ResolutionLimits
from ai_trainer.application.ports.exercises import (
    ExerciseHistoryPort,
    ExerciseLookupPort,
    ExerciseNamesPort,
)
from ai_trainer.domain.exercises import ExerciseDetail, LoggedName
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID

SHORTLIST_MAX = 4
# The measure a name is first matched against; a shortlist only ranks by it.
_DEFAULT_MEASURE = "load_reps"
_LANG = "en"


class ExerciseLookup(BaseModel):
    """Where the exercises came from: `history` is the one the athlete settled before,
    `catalogue` a shortlist for a name they have not. `logged` is what the athlete has logged
    that fits the name, whichever way they named it (B40)."""

    model_config = ConfigDict(frozen=True)

    source: Literal["history", "catalogue"]
    exercises: list[ExerciseDetail]
    logged: list[LoggedName] = []


class ExerciseLookupFailed(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: Literal["blank_name", "no_match"]


class ExerciseFinder:
    """`names` shortlists a name history doesn't know; the composition root passes the same one a
    log uses, the catalogue search included (#508). It defaults to `exercises`."""

    def __init__(
        self,
        exercises: ExerciseLookupPort,
        limits: ResolutionLimits,
        history: ExerciseHistoryPort,
        *,
        names: ExerciseNamesPort | None = None,
    ) -> None:
        self._exercises = exercises
        self._limits = limits
        self._history = history
        self._names = names or exercises

    async def find(self, user_id: UUID, name: str) -> ExerciseLookup | ExerciseLookupFailed:
        typed = name.strip()
        if not typed:
            return ExerciseLookupFailed(reason="blank_name")
        # Near only: a looser match would report another exercise's numbers as this one's.
        logged = list(
            await self._history.logged_names(
                user_id, typed, near=self._limits.near_match, limit=SHORTLIST_MAX
            )
        )
        settled = await self._exercises.match_user_aliases(
            user_id, sport_id=GYM_SPORT_ID, names=[typed], near=self._limits.near_match
        )
        if typed in settled:
            found = await self._exercises.describe(user_id, [settled[typed]], lang=_LANG)
            if found:
                return ExerciseLookup(source="history", exercises=list(found), logged=logged)
        candidates = await self._names.shortlist(
            user_id,
            sport_id=GYM_SPORT_ID,
            name=typed,
            measure=_DEFAULT_MEASURE,
            floor=self._limits.candidate_floor,
            limit=min(self._limits.shortlist_size, SHORTLIST_MAX),
        )
        described = await self._exercises.describe(
            user_id, [candidate.id for candidate in candidates], lang=_LANG
        )
        if not described and not logged:
            return ExerciseLookupFailed(reason="no_match")
        return ExerciseLookup(source="catalogue", exercises=list(described), logged=logged)
