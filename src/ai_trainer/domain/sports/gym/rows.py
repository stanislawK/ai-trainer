"""Gym payload read into the typed rows of ADR-0032 (tickets #376 and #395), and what the rows
give back to the extraction. Only the gym's `ActivityStore` writes these rows; a draft stays
JSONB. A value the message didn't give stays `None`, never estimated (B38, ADR-0032 invariant
4)."""

import re
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.domain.sports.gym.normalize import to_kg
from ai_trainer.domain.sports.gym.payload import Exercise, GymPayload, GymSet, LoadUnit, Measure

# The first number in a load as typed: "45kg", "22,5 kg", "225 lb".
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


class GymSetRow(BaseModel):
    """One `gym_sets` row: "3 x 8" is three equal rows."""

    model_config = ConfigDict(frozen=True)

    set_no: int
    reps: int | None
    load_kg: Decimal | None
    load_original: str | None
    duration_s: int | None = None
    distance_m: Decimal | None = None
    band_label: str | None = None


class GymEntryRow(BaseModel):
    """One `gym_exercise_entries` row and its sets, in the order logged."""

    model_config = ConfigDict(frozen=True)

    exercise_id: UUID | None = None
    name_as_typed: str
    measure: Measure
    per_side: bool = False
    implements: int | None = None
    rir: int | None
    sets: tuple[GymSetRow, ...]


def _set(set_no: int, gym_set: GymSet) -> GymSetRow:
    load = gym_set.load
    distance = gym_set.distance_m
    return GymSetRow(
        set_no=set_no,
        reps=gym_set.reps,
        load_kg=None if load is None else Decimal(str(load.kg)),
        load_original=None if load is None else load.original,
        duration_s=gym_set.duration_s,
        distance_m=None if distance is None else Decimal(str(distance)),
        band_label=gym_set.band_label,
    )


def _entry(exercise: Exercise) -> GymEntryRow:
    return GymEntryRow(
        exercise_id=exercise.exercise_id,
        name_as_typed=exercise.name_as_typed,
        measure=exercise.measure,
        per_side=exercise.per_side,
        implements=exercise.implements,
        rir=exercise.rir,
        sets=tuple(_set(set_no, gym_set) for set_no, gym_set in enumerate(exercise.sets, start=1)),
    )


def entries_of(payload: GymPayload) -> tuple[GymEntryRow, ...]:
    return tuple(_entry(exercise) for exercise in payload.activities)


def unit_of(load_original: str, load_kg: Decimal) -> LoadUnit | None:
    """The unit a stored load was typed in, read back from its first typed number and its kg,
    for the recent exercises' unit hint (#206); `None` when neither unit gives that kg.
    Owner-chosen at #376's plan gate: the rows keep kg and the text, not the unit."""
    number = _NUMBER.search(load_original)
    if number is None:
        return None
    value = float(number.group().replace(",", "."))
    kg = float(load_kg)
    if to_kg(value, "kg") == kg:
        return "kg"
    if to_kg(value, "lb") == kg:
        return "lb"
    return None


class RecentExercise(BaseModel):
    """An exercise of a recent session as the rows give it back: the name as typed and the
    unit its load was in."""

    name: str
    unit: LoadUnit | None = None


class RecentGymSession(BaseModel):
    activities: list[RecentExercise]
