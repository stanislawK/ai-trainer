"""`find_exercise` (B37, ADR-0031, ADR-0010, ticket #401): the athlete's own answer to "which
exercise is this name", read-only and in code, with no model call. History first, as naming does:
a name the athlete already settled comes back alone. Otherwise a shortlist of at most
`SHORTLIST_MAX` catalogue or custom exercises. A name that fits nothing is an
`ExerciseLookupFailed` value, never a raise, so a tool can turn it into a recovery hint."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.application.exercise_resolution import ResolutionLimits
from ai_trainer.application.ports.exercises import ExerciseLookupPort
from ai_trainer.domain.exercises import ExerciseDetail
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID

SHORTLIST_MAX = 4
# The measure a name is first matched against; a shortlist only ranks by it.
_DEFAULT_MEASURE = "load_reps"
_LANG = "en"


class ExerciseLookup(BaseModel):
    """Where the exercises came from: `history` is the one the athlete settled before,
    `catalogue` a shortlist for a name they have not."""

    model_config = ConfigDict(frozen=True)

    source: Literal["history", "catalogue"]
    exercises: list[ExerciseDetail]


class ExerciseLookupFailed(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: Literal["blank_name", "no_match"]


class ExerciseFinder:
    def __init__(self, exercises: ExerciseLookupPort, limits: ResolutionLimits) -> None:
        self._exercises = exercises
        self._limits = limits

    async def find(self, user_id: UUID, name: str) -> ExerciseLookup | ExerciseLookupFailed:
        typed = name.strip()
        if not typed:
            return ExerciseLookupFailed(reason="blank_name")
        settled = await self._exercises.match_user_aliases(
            user_id, sport_id=GYM_SPORT_ID, names=[typed], near=self._limits.near_match
        )
        if typed in settled:
            found = await self._exercises.describe(user_id, [settled[typed]], lang=_LANG)
            if found:
                return ExerciseLookup(source="history", exercises=list(found))
        candidates = await self._exercises.shortlist(
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
        if not described:
            return ExerciseLookupFailed(reason="no_match")
        return ExerciseLookup(source="catalogue", exercises=list(described))
