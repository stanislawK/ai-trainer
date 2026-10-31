"""Gym payload v2 (ADR-0006, ADR-0022, ADR-0032). Only this model reads or writes a gym draft's
`payload`; a change bumps `GYM_PAYLOAD_VERSION` and older versions are upcast on read
(ADR-0022 invariant 4). A value the message didn't give stays empty (B38)."""

from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

GYM_PAYLOAD_VERSION = 2

type LoadUnit = Literal["kg", "lb"]
# `gym_exercise_entries.measure`.
type Measure = Literal["load_reps", "reps", "time", "distance", "band_reps"]
# An exercise as the athlete named it ("squat", "RDL", "przysiad"), never blank.
type ExerciseName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# A resistance band as the athlete named it ("red", "czerwona"), never ranked or converted (B39).
type BandLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Reps in reserve: 0 is to failure; past 10 it says nothing about effort.
MAX_RIR = 10
# `gym_exercise_entries.implements`: "two dumbbells" is 2.
MAX_IMPLEMENTS = 4


class Load(BaseModel):
    """External load as the athlete typed it; bodyweight work has none. `original`, `value`
    and `unit` are the truth; `kg` is derived by code, never by the model."""

    model_config = ConfigDict(frozen=True)

    original: str = Field(min_length=1)
    value: float = Field(gt=0)
    unit: LoadUnit
    # A tiny typed load can round to 0 kg; only what was typed must be above 0.
    kg: float = Field(ge=0)


class GymSet(BaseModel):
    """One set, with what the athlete gave for it. `load` is per implement. Every field empty
    is a set with no measure: "4 sets", reps unknown. Seconds and metres are derived by code,
    and like kg a tiny typed value can round to 0."""

    model_config = ConfigDict(frozen=True)

    reps: int | None = Field(default=None, ge=1)
    load: Load | None = None
    duration_s: int | None = Field(default=None, ge=0)
    distance_m: float | None = Field(default=None, ge=0)
    band_label: BandLabel | None = None


def measure_of(gym_set: GymSet) -> Measure:
    """What a set is measured by, read in code from what it holds, never by the model."""
    if gym_set.duration_s is not None:
        return "time"
    if gym_set.distance_m is not None:
        return "distance"
    if gym_set.band_label is not None:
        return "band_reps"
    return "reps" if gym_set.load is None else "load_reps"


class _ExerciseV1(BaseModel):
    """A v1 exercise, one scheme: `sets` is a count, and `reps` and `load` are per set."""

    name: ExerciseName
    sets: int | None = Field(default=None, ge=1)
    reps: int | None = Field(default=None, ge=1)
    load: Load | None = None
    rir: int | None = Field(default=None, ge=0, le=MAX_RIR)

    def upcast(self) -> dict[str, object]:
        gym_set = GymSet(reps=self.reps, load=self.load)
        return {
            "name_as_typed": self.name,
            "measure": measure_of(gym_set),
            "rir": self.rir,
            # No set count is one set with what was given (ADR-0032, #376's plan gate).
            "sets": [gym_set] * (self.sets or 1),
        }


class Exercise(BaseModel):
    """One exercise at one scheme: the same lift at another weight or rep count is another
    entry. `exercise_id` stays empty until the name is settled (ADR-0031). `implements` of 2
    with a load means that load in each hand."""

    exercise_id: UUID | None = None
    name_as_typed: ExerciseName
    measure: Measure
    per_side: bool = False
    implements: int | None = Field(default=None, ge=1, le=MAX_IMPLEMENTS)
    rir: int | None = Field(default=None, ge=0, le=MAX_RIR)
    sets: list[GymSet] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _upcast_v1(cls, data: object) -> object:
        # By shape, as the two have no name in common: a reader of a draft has no version to
        # hand, and an added log moves a session's version column while its JSON stays v1.
        if isinstance(data, Mapping) and "name" in data:
            return _ExerciseV1.model_validate(data).upcast()
        return data


class GymPayload(BaseModel):
    sport: Literal["gym"] = "gym"
    activities: list[Exercise] = Field(min_length=1)
