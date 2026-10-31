"""Gym payload v1 read into the typed rows of ADR-0032 (ticket #376), and what the rows give
back to the extraction. Only the gym's `ActivityStore` writes these rows; a draft stays JSONB.
A value the message didn't give stays `None`, never estimated (B38, ADR-0032 invariant 4)."""

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ai_trainer.domain.sports.gym.normalize import to_kg
from ai_trainer.domain.sports.gym.payload import Exercise, GymPayload, LoadUnit

# `gym_exercise_entries.measure`; v1 can only say load and reps, or reps.
type Measure = Literal["load_reps", "reps", "time", "distance", "band_reps"]

# The first number in a load as typed: "45kg", "22,5 kg", "225 lb".
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


class GymSetRow(BaseModel):
    """One `gym_sets` row: "3 x 8" is three equal rows."""

    model_config = ConfigDict(frozen=True)

    set_no: int
    reps: int | None
    load_kg: Decimal | None
    load_original: str | None


class GymEntryRow(BaseModel):
    """One `gym_exercise_entries` row and its sets, in the order logged."""

    model_config = ConfigDict(frozen=True)

    name_as_typed: str
    measure: Measure
    rir: int | None
    sets: tuple[GymSetRow, ...]


def _entry(exercise: Exercise) -> GymEntryRow:
    load = exercise.load
    # No set count is one set with what was given, owner-chosen at #376's plan gate: rows can't
    # say "an unknown number of sets", and none would drop the reps and load.
    count = exercise.sets or 1
    return GymEntryRow(
        name_as_typed=exercise.name,
        measure="reps" if load is None else "load_reps",
        rir=exercise.rir,
        sets=tuple(
            GymSetRow(
                set_no=set_no,
                reps=exercise.reps,
                load_kg=None if load is None else Decimal(str(load.kg)),
                load_original=None if load is None else load.original,
            )
            for set_no in range(1, count + 1)
        ),
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
