"""`ResolveExercises` (B37, ADR-0031, ticket #397): names a gym draft's exercises in code, on the
logging hot path, with no model call (G14). History comes first: the athlete's own aliases, exact
or near. A name still open gets a question on the draft's card. An earlier entry that carries an
`exercise_id` needs no lookup of its own: its alias was stored in the transaction that linked it,
so the aliases already answer for it. Nothing here writes: a tap changes the draft, and only the
confirm stores an alias (`adapters/exercise_names.py`)."""

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.application.ports.conversation import FixedReply
from ai_trainer.application.ports.exercises import ExerciseNamesPort
from ai_trainer.application.reply.shared import LOG_KIND
from ai_trainer.domain.clarification import KEEP_NAME_OPTION, ChoiceOption, Clarification
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.domain.sports.gym import names as gym_names
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID
from ai_trainer.domain.sports.gym.payload import GymPayload

# Their copy is in `components/gym_copy.html`.
_CANDIDATE_COPY_KEY = "gym_exercise_candidate"
_KEEP_COPY_KEY = "gym_exercise_keep"


@dataclass(frozen=True, slots=True)
class ResolutionLimits:
    """From `Settings`: a near match to an alias, a candidate's floor and the shortlist's size."""

    near_match: float
    candidate_floor: float
    shortlist_size: int


def _candidate_option(candidate: ExerciseCandidate) -> ChoiceOption:
    told_apart_by = (candidate.equipment, candidate.target)
    return ChoiceOption(
        id=str(candidate.id),
        copy_key=_CANDIDATE_COPY_KEY,
        params={
            "name": candidate.name,
            "detail": ", ".join(part for part in told_apart_by if part),
        },
    )


class ResolveExercises:
    """Implements `DraftSettlerPort` for the gym."""

    def __init__(
        self,
        names: ExerciseNamesPort,
        *,
        question: Callable[[], FixedReply],
        limits: ResolutionLimits,
    ) -> None:
        self._names = names
        self._question = question
        self._limits = limits

    async def settle(self, user_id: UUID, payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        draft = GymPayload.model_validate(payload)
        still_open = gym_names.unsettled(draft)
        if not still_open:
            return draft.model_dump(mode="json")
        # Every spelling on the draft, since one name's entries may be typed two ways.
        spellings = gym_names.spellings_of(draft, still_open)
        matched = await self._names.match_user_aliases(
            user_id, sport_id=GYM_SPORT_ID, names=spellings, near=self._limits.near_match
        )
        return gym_names.with_exercises(draft, matched).model_dump(mode="json")

    async def next_question(
        self, user_id: UUID, payload: Mapping[str, JsonValue], *, settled: Collection[str] = ()
    ) -> Clarification | None:
        still_open = gym_names.unsettled(GymPayload.model_validate(payload), settled=settled)
        if not still_open:
            return None
        exercise = still_open[0]
        candidates = await self._names.shortlist(
            user_id,
            sport_id=GYM_SPORT_ID,
            name=exercise.name_as_typed,
            measure=exercise.measure,
            floor=self._limits.candidate_floor,
            limit=self._limits.shortlist_size,
        )
        return Clarification(
            question=self._question().text,
            options=(
                *(_candidate_option(candidate) for candidate in candidates),
                ChoiceOption(
                    id=KEEP_NAME_OPTION,
                    copy_key=_KEEP_COPY_KEY,
                    params={"name": exercise.name_as_typed},
                ),
            ),
            pending=LOG_KIND,
            subject=exercise.name_as_typed,
        )

    def answered(
        self, payload: Mapping[str, JsonValue], subject: str, option_id: str
    ) -> dict[str, JsonValue]:
        draft = GymPayload.model_validate(payload)
        return gym_names.answered(draft, subject, option_id).model_dump(mode="json")

    def mentions(self, payload: Mapping[str, JsonValue], subject: str) -> bool:
        return gym_names.mentions(GymPayload.model_validate(payload), subject)
