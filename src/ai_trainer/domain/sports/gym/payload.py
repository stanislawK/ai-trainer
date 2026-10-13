"""Gym payload v1 (ADR-0006, ADR-0022). Only this model reads or writes a gym session's
`payload`; a change bumps `GYM_PAYLOAD_VERSION` and older versions are upcast on read
(ADR-0022 invariant 4)."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

GYM_PAYLOAD_VERSION = 1

type LoadUnit = Literal["kg", "lb"]
# An exercise as the athlete named it ("squat", "RDL", "przysiad"), never blank.
type ExerciseName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Reps in reserve: 0 is to failure; past 10 it says nothing about effort.
MAX_RIR = 10


class Load(BaseModel):
    """External load as the athlete typed it; bodyweight work has none. `original`, `value`
    and `unit` are the truth; `kg` is derived by code, never by the model."""

    model_config = ConfigDict(frozen=True)

    original: str = Field(min_length=1)
    value: float = Field(gt=0)
    unit: LoadUnit
    # A tiny typed load can round to 0 kg; only what was typed must be above 0.
    kg: float = Field(ge=0)


class Exercise(BaseModel):
    """One exercise at one scheme: the same lift at another weight or rep count is another
    entry. `reps` and `load` are per set."""

    name: ExerciseName
    sets: int | None = Field(default=None, ge=1)
    reps: int | None = Field(default=None, ge=1)
    load: Load | None = None
    rir: int | None = Field(default=None, ge=0, le=MAX_RIR)


class GymPayload(BaseModel):
    sport: Literal["gym"] = "gym"
    activities: list[Exercise] = Field(min_length=1)
