"""`ResolveExercises` (B37, ADR-0031, tickets #397 and #399): names a gym draft's exercises in
code, on the logging hot path (G14). History comes first: the athlete's own aliases, exact or
near. A name history doesn't know goes to Jev with its shortlist, one call per name: a confident
pick fills the draft with no card. Whatever is still open gets a question on the draft's card.
An earlier entry that carries an `exercise_id` needs no lookup of its own: its alias was stored
in the transaction that linked it, so the aliases already answer for it. Nothing here writes: a
pick or a tap changes the draft, and only the confirm stores an alias
(`adapters/exercise_names.py`)."""

import asyncio
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.application.ports.conversation import FixedReply
from ai_trainer.application.ports.draft_settling import DecisionRef
from ai_trainer.application.ports.exercises import ExerciseNamesPort, ExercisePickerPort
from ai_trainer.application.reply.shared import LOG_KIND
from ai_trainer.domain.clarification import KEEP_NAME_OPTION, ChoiceOption, Clarification
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseLabel
from ai_trainer.domain.sports.gym import names as gym_names
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID
from ai_trainer.domain.sports.gym.payload import Exercise, GymPayload

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
    return ChoiceOption(
        id=str(candidate.id),
        copy_key=_CANDIDATE_COPY_KEY,
        params={"name": candidate.name, "how": candidate.how or ""},
    )


def _label_of(typed: str, label: ExerciseLabel, *, answered: bool) -> ExerciseLabel:
    """ "you usually log this as …" for a near match the athlete didn't answer about; otherwise
    "you wrote: …", unless the typed name already reads as the exercise's (F23, ticket #402)."""
    if label.usual is not None and not answered:
        return label
    return label.model_copy(
        update={"usual": None, "wrote": None if gym_names.reads_as(typed, label.name) else typed}
    )


class ResolveExercises:
    """Implements `DraftSettlerPort` for the gym."""

    def __init__(
        self,
        names: ExerciseNamesPort,
        picker: ExercisePickerPort,
        *,
        question: Callable[[], FixedReply],
        limits: ResolutionLimits,
    ) -> None:
        self._names = names
        self._picker = picker
        self._question = question
        self._limits = limits

    async def settle(
        self, user_id: UUID, payload: Mapping[str, JsonValue], *, pick: bool = True
    ) -> dict[str, JsonValue]:
        draft = GymPayload.model_validate(payload)
        still_open = gym_names.unsettled(draft)
        if not still_open:
            return draft.model_dump(mode="json")
        # Every spelling on the draft, since one name's entries may be typed two ways.
        spellings = gym_names.spellings_of(draft, still_open)
        matched = await self._names.match_user_aliases(
            user_id, sport_id=GYM_SPORT_ID, names=spellings, near=self._limits.near_match
        )
        draft = gym_names.with_exercises(draft, matched)
        if pick:
            draft = await self._with_picks(user_id, draft)
        return draft.model_dump(mode="json")

    async def _with_picks(self, user_id: UUID, draft: GymPayload) -> GymPayload:
        """The draft with the exercise Jev confidently picked on every entry of each name
        history left open. The names are asked concurrently, so the draft waits for the slowest
        call, which its timeout bounds."""
        still_open = gym_names.unsettled(draft)
        picks = await asyncio.gather(*(self._pick(user_id, exercise) for exercise in still_open))
        chosen = {
            spelling: candidate.id
            for exercise, candidate in zip(still_open, picks, strict=True)
            if candidate is not None
            for spelling in gym_names.spellings_of(draft, [exercise])
        }
        return gym_names.with_exercises(draft, chosen, jev_pick=True) if chosen else draft

    async def _pick(self, user_id: UUID, exercise: Exercise) -> ExerciseCandidate | None:
        candidates = await self._shortlist(user_id, exercise)
        if not candidates:
            return None  # Jev is never asked to pick from nothing.
        return await self._picker.pick(user_id, exercise.name_as_typed, candidates)

    async def _shortlist(self, user_id: UUID, exercise: Exercise) -> Sequence[ExerciseCandidate]:
        return await self._names.shortlist(
            user_id,
            sport_id=GYM_SPORT_ID,
            name=exercise.name_as_typed,
            measure=exercise.measure,
            floor=self._limits.candidate_floor,
            limit=self._limits.shortlist_size,
        )

    def overridden_picks(
        self, before: Mapping[str, JsonValue], after: Mapping[str, JsonValue]
    ) -> Sequence[DecisionRef]:
        undone = gym_names.overridden_picks(
            GymPayload.model_validate(before), GymPayload.model_validate(after)
        )
        if not undone:
            return ()
        return (
            DecisionRef(self._picker.decision_id, self._picker.decision_version, ("exercise_id",)),
        )

    async def next_question(
        self, user_id: UUID, payload: Mapping[str, JsonValue], *, settled: Collection[str] = ()
    ) -> Clarification | None:
        still_open = gym_names.unsettled(GymPayload.model_validate(payload), settled=settled)
        if not still_open:
            return None
        exercise = still_open[0]
        candidates = await self._shortlist(user_id, exercise)
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

    def typed(
        self, payload: Mapping[str, JsonValue], subject: str, name: str
    ) -> dict[str, JsonValue]:
        draft = GymPayload.model_validate(payload)
        return gym_names.typed(draft, subject, name).model_dump(mode="json")

    async def labels(
        self, user_id: UUID, payload: Mapping[str, JsonValue], *, answered: Collection[str] = ()
    ) -> Mapping[str, ExerciseLabel]:
        named = gym_names.named(GymPayload.model_validate(payload))
        if not named:
            return {}
        found = await self._names.labels(user_id, named=named, near=self._limits.near_match)
        return {
            typed: _label_of(
                typed, label, answered=any(gym_names.reads_as(typed, name) for name in answered)
            )
            for typed, label in found.items()
        }

    def mentions(self, payload: Mapping[str, JsonValue], subject: str) -> bool:
        return gym_names.mentions(GymPayload.model_validate(payload), subject)

    def names(self, payload: Mapping[str, JsonValue]) -> frozenset[str]:
        return gym_names.keys_of(GymPayload.model_validate(payload))
