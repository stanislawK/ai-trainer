"""What `gym.extract` returns (ADR-0008, ADR-0014): an absolute date, how it was found, and
each exercise with its sets, reps, load exactly as typed and RIR. The model copies the numbers
and text and picks the unit; code, not the model, converts to kg. A log it can't read safely is
a question instead: which movement an exercise name means, or which unit a load typed without
one is in, its readings likeliest first (B17, ticket #206)."""

import re
from collections.abc import Mapping, Sequence
from typing import Literal, Self, get_args

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator

from ai_trainer.domain.clarification import ChoiceOption
from ai_trainer.domain.sports.base import ExtractionQuestion, SessionExtraction
from ai_trainer.domain.sports.gym.normalize import normalize_load
from ai_trainer.domain.sports.gym.payload import (
    MAX_RIR,
    Exercise,
    ExerciseName,
    GymPayload,
    LoadUnit,
)

# A movement option's other readings, in one param (params are strings); no name holds a newline.
_OTHERS_SEPARATOR = "\n"
# How `recent_exercises` marks an entry's unit.
_RECENT_UNIT = re.compile(r"\s*\((?:kg|lb)\)\s*$")
# A load typed with its unit has letters in it ("60kg", "225 lb", "60 funtów"); "60" has none.
_UNIT_TEXT = re.compile(r"[^\W\d_]")


class ExtractedLoad(BaseModel):
    original: str = Field(min_length=1)
    value: float = Field(gt=0)
    # None when the message gives no unit ("@ 60") and the athlete's recent sessions don't
    # settle it; it is read as kg, owner-chosen at #159's plan gate and confirmed at #206's (all
    # athletes are in Europe), and the athlete can change it before confirming.
    unit: LoadUnit | None = None


class ExtractedExercise(BaseModel):
    name: ExerciseName
    sets: int | None = Field(default=None, ge=1)
    reps: int | None = Field(default=None, ge=1)
    load: ExtractedLoad | None = None
    rir: int | None = Field(default=None, ge=0, le=MAX_RIR)


# The copy key of a movement option, and the prefix of a unit option's, in
# `components/gym_copy.html`.
_MOVEMENT_COPY_KEY = "gym_reading_movement"
_UNIT_COPY_KEY = "gym_reading_"


class GymExtraction(SessionExtraction):
    activities: list[ExtractedExercise] = Field(min_length=1)

    def answered(self, answer: ChoiceOption) -> Self:
        """A movement pick names every exercise typed as the question asked, or written as one
        of its other readings by the resumed run; a unit pick sets
        the unit of every load typed without one, also where the resumed run gave it one. Every
        other exercise stays as the message said. An option that is no reading changes
        nothing."""
        exercise = answer.params.get("exercise")
        movement = answer.params.get("movement")
        if exercise is not None and movement is not None:
            others = answer.params.get("others", "").split(_OTHERS_SEPARATOR)
            return self._named((exercise, *others), movement)
        if answer.id in get_args(LoadUnit.__value__) and "load" in answer.params:
            return self._in_unit(answer.id)
        return self

    def _named(self, asked_as: Sequence[str], movement: str) -> Self:
        asked = {_key(name) for name in asked_as if name}
        return self.model_copy(
            update={
                "activities": [
                    entry.model_copy(update={"name": movement})
                    if _key(entry.name) in asked
                    else entry
                    for entry in self.activities
                ]
            }
        )

    def _in_unit(self, unit: str) -> Self:
        return self.model_copy(
            update={
                "activities": [
                    entry.model_copy(update={"load": entry.load.model_copy(update={"unit": unit})})
                    if entry.load is not None and not _UNIT_TEXT.search(entry.load.original)
                    else entry
                    for entry in self.activities
                ]
            }
        )

    def to_payload(self) -> GymPayload:
        return GymPayload(
            activities=[
                Exercise(
                    name=exercise.name,
                    sets=exercise.sets,
                    reps=exercise.reps,
                    load=(
                        normalize_load(
                            exercise.load.original, exercise.load.value, exercise.load.unit or "kg"
                        )
                        if exercise.load is not None
                        else None
                    ),
                    rir=exercise.rir,
                )
                for exercise in self.activities
            ]
        )


def _key(name: str) -> str:
    return name.strip().casefold()


class GymQuestion(ExtractionQuestion):
    """`exercise` is the name as typed; `readings` are the movements it may mean, or, for a
    load typed without its unit (`load`, as typed), the units it may be in."""

    about: Literal["movement", "unit"]
    exercise: ExerciseName
    load: str | None = None
    readings: list[ExerciseName] = Field(min_length=2)

    # A tap matches the typed name whatever its case (`GymExtraction.answered`), and a
    # movement's label starts upper case on the card, so the model's casing says nothing.
    @field_validator("exercise")
    @classmethod
    def _exercise_in_lower_case(cls, exercise: str) -> str:
        return exercise.casefold()

    @field_validator("readings")
    @classmethod
    def _readings_in_lower_case(cls, readings: list[str]) -> list[str]:
        # A movement copied from the recent sessions keeps no unit ("overhead press (kg)").
        return [_RECENT_UNIT.sub("", reading).strip().casefold() for reading in readings]

    @model_validator(mode="after")
    def _distinct_readings_of_what_it_asks(self) -> Self:
        # Each reading is an option's ID, which the athlete's tap sends back (ADR-0015
        # invariant 4).
        if len({_key(reading) for reading in self.readings}) != len(self.readings):
            raise ValueError("each reading needs to be distinct")
        if self.about == "unit":
            if not set(self.readings) <= set(get_args(LoadUnit.__value__)):
                raise ValueError("a unit question reads only kg or lb")
            if self.load is None or not self.load.strip() or _UNIT_TEXT.search(self.load):
                raise ValueError("a unit question needs the load as typed, without a unit")
        return self

    def options(self) -> tuple[ChoiceOption, ...]:
        if self.about == "movement":
            return tuple(
                ChoiceOption(
                    id=movement,
                    copy_key=_MOVEMENT_COPY_KEY,
                    params={
                        "exercise": self.exercise,
                        "movement": movement,
                        "others": _OTHERS_SEPARATOR.join(
                            other for other in self.readings if other != movement
                        ),
                    },
                )
                for movement in self.readings
            )
        assert self.load is not None  # checked above
        return tuple(
            ChoiceOption(
                id=unit,
                copy_key=f"{_UNIT_COPY_KEY}{unit}",
                params={"exercise": self.exercise, "load": self.load.strip()},
            )
            for unit in self.readings
        )


def recent_exercises(payloads: Sequence[Mapping[str, JsonValue]]) -> tuple[str, ...]:
    """The exercises of the athlete's recent sessions, each once with the unit its load was in
    ("bench press (lb)"), from the most recent session first (ticket #206)."""
    seen: dict[tuple[str, str | None], str] = {}
    for payload in payloads:
        for exercise in GymPayload.model_validate(payload).activities:
            unit = None if exercise.load is None else exercise.load.unit
            entry = exercise.name if unit is None else f"{exercise.name} ({unit})"
            seen.setdefault((_key(exercise.name), unit), entry)
    return tuple(seen.values())
