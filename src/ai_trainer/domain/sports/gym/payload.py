"""Gym payload v2 (ADR-0006, ADR-0022, ADR-0032). Only this model reads or writes a gym draft's
`payload`; a change bumps `GYM_PAYLOAD_VERSION` and older versions are upcast on read
(ADR-0022 invariant 4). A value the message didn't give stays empty (B38)."""

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# Version 3 added `keep_name`; a v2 document reads as it is, with the flag off (ticket #397).
# Version 4 added `jev_pick`, the same way (ticket #399).
# Version 5 added `candidate_scores`, the same way, with no scores (ticket #579).
# Version 6 added the `distance_time` measure: a `time` or `distance` exercise whose first set
# with a value holds both reads as `distance_time` (ticket #626).
# Version 7 added a set's machine settings, `incline_pct`, `machine_level` and `watts`; a v5 or
# v6 document reads as it is, with none of them (ticket #630).
GYM_PAYLOAD_VERSION = 7

type LoadUnit = Literal["kg", "lb"]
# `gym_exercise_entries.measure`.
type Measure = Literal["load_reps", "reps", "time", "distance", "distance_time", "band_reps"]
# An exercise as the athlete named it ("squat", "RDL", "przysiad"), never blank.
type ExerciseName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# A resistance band as the athlete named it ("red", "czerwona"), never ranked or converted (B39).
type BandLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Reps in reserve: 0 is to failure; past 10 it says nothing about effort.
MAX_RIR = 10
# `gym_exercise_entries.implements`: "two dumbbells" is 2.
MAX_IMPLEMENTS = 4
# A count becomes one row each, so it can't be left open.
MAX_SETS = 100
# A set past a day is a typo; a distance stops at what `gym_sets.distance_m` holds.
MAX_DURATION_S = 86_400
MAX_DISTANCE_M = 9_999_999.9
# A machine's settings, bounded as `gym_sets`' CHECKs are (ADR-0032 #618 amendment): incline in
# %, which a decline takes below 0, the machine's level, and power in watts.
MIN_INCLINE_PCT = -10
MAX_INCLINE_PCT = 40
MAX_MACHINE_LEVEL = 50
MAX_WATTS = 2000


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
    duration_s: int | None = Field(default=None, ge=0, le=MAX_DURATION_S)
    distance_m: float | None = Field(default=None, ge=0, le=MAX_DISTANCE_M)
    band_label: BandLabel | None = None
    # Machine settings, only as the athlete states them (B44); they hold no measure.
    incline_pct: float | None = Field(
        default=None, ge=MIN_INCLINE_PCT, le=MAX_INCLINE_PCT, allow_inf_nan=False
    )
    machine_level: int | None = Field(default=None, ge=1, le=MAX_MACHINE_LEVEL)
    watts: int | None = Field(default=None, ge=1, le=MAX_WATTS)


_SETTINGS = {"incline_pct": None, "machine_level": None, "watts": None}


def first_given(sets: Sequence[GymSet]) -> GymSet | None:
    """The first set that holds a measure, which an exercise's measure is read from. Machine
    settings alone hold none, so a set with only them counts as blank (ADR-0032 #618
    amendment)."""
    return next(
        (gym_set for gym_set in sets if gym_set.model_copy(update=_SETTINGS) != GymSet()), None
    )


def measure_of(gym_set: GymSet) -> Measure:
    """What a set is measured by, read in code from what it holds, never by the model."""
    if gym_set.duration_s is not None and gym_set.distance_m is not None:
        return "distance_time"
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
    sets: int | None = Field(default=None, ge=1, le=MAX_SETS)
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
    """One exercise with its sets: a pyramid is one entry of several sets, which extraction
    builds by merging neighbouring schemes of the same lift (ticket #410). `exercise_id` stays
    empty until the name is settled (ADR-0031), and `keep_name` says the athlete kept their own
    name, which a confirm makes a custom exercise (ticket #397). `jev_pick` says `exercise_id`
    is Jev's pick rather than the athlete's own answer, so an edit that undoes it is a
    `decision_overridden` signal (ticket #399). `candidate_scores` is Jev's probability per
    shortlisted exercise for a name it didn't settle, which orders the card with no second call;
    a settled or renamed entry has none (ticket #579).
    `implements` of 2 with a load means that load in each hand."""

    exercise_id: UUID | None = None
    keep_name: bool = False
    jev_pick: bool = False
    candidate_scores: dict[UUID, float] = {}
    name_as_typed: ExerciseName
    measure: Measure
    per_side: bool = False
    implements: int | None = Field(default=None, ge=1, le=MAX_IMPLEMENTS)
    rir: int | None = Field(default=None, ge=0, le=MAX_RIR)
    sets: list[GymSet] = Field(min_length=1, max_length=MAX_SETS)

    @model_validator(mode="before")
    @classmethod
    def _upcast_v1(cls, data: object) -> object:
        # By shape, as the two have no name in common: a reader of a draft has no version to
        # hand, and an added log moves a session's version column while its JSON stays v1.
        if isinstance(data, Mapping) and "name" in data:
            return _ExerciseV1.model_validate(data).upcast()
        return data

    @model_validator(mode="after")
    def _upcast_v5(self) -> Exercise:
        # A v5 draft read a timed distance as `time` (ticket #626); no version is in hand here
        # either, so it goes by what the sets hold, as the migration does for saved entries.
        given = first_given(self.sets)
        if self.measure in ("time", "distance") and given and measure_of(given) == "distance_time":
            self.measure = "distance_time"
        return self


class GymPayload(BaseModel):
    sport: Literal["gym"] = "gym"
    activities: list[Exercise] = Field(min_length=1)
