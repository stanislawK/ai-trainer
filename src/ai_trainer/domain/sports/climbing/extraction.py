"""What `climbing.extract` returns (ADR-0008, ADR-0014): an absolute date, how it was found,
and each activity with its grade exactly as typed. Code, not the model, decides the scale. A
log it can't read safely is a question instead: its readings, likeliest first (B17, ticket
#164)."""

from collections.abc import Mapping, Sequence
from typing import Self, get_args

from pydantic import BaseModel, Field, JsonValue, field_validator

from ai_trainer.domain.clarification import ChoiceOption
from ai_trainer.domain.sports.base import ExtractionQuestion, SessionExtraction
from ai_trainer.domain.sports.climbing.normalize import grade_in_scale, normalize_grade
from ai_trainer.domain.sports.climbing.payload import (
    AscentStyle,
    ClimbingActivity,
    ClimbingKind,
    ClimbingPayload,
    RouteRef,
)


class ExtractedClimb(BaseModel):
    kind: ClimbingKind
    count: int = Field(ge=1)
    grade: str | None = None
    style: AscentStyle | None = None
    attempts: int | None = Field(default=None, ge=1)
    route: RouteRef | None = None

    @field_validator("grade")
    @classmethod
    def _blank_is_no_grade(cls, value: str | None) -> str | None:
        return value if value is None or value.strip() else None


class ClimbingExtraction(SessionExtraction):
    activities: list[ExtractedClimb] = Field(min_length=1)

    def answered(self, answer: ChoiceOption) -> Self:
        """The climbs the reading question was about, those at the grade its option shows,
        take the picked kind; every other climb stays as the message said. The option shows
        the grade in its kind's scale, so letter case is not compared. An option that is no
        reading changes nothing."""
        grade = answer.params.get("grade")
        if answer.id not in get_args(ClimbingKind.__value__) or grade is None:
            return self
        return self.model_copy(
            update={
                "activities": [
                    climb.model_copy(update={"kind": answer.id})
                    if climb.grade is not None and climb.grade.strip().lower() == grade.lower()
                    else climb
                    for climb in self.activities
                ]
            }
        )

    def to_payload(self) -> ClimbingPayload:
        return ClimbingPayload(
            activities=[
                ClimbingActivity(
                    kind=climb.kind,
                    count=climb.count,
                    grade=normalize_grade(climb.grade, climb.kind)
                    if climb.grade is not None
                    else None,
                    style=climb.style,
                    attempts=climb.attempts,
                    route=climb.route,
                )
                for climb in self.activities
            ]
        )


class ClimbingReading(BaseModel):
    """One way to read the log: what kind of climbing it was, and the grade that reading turns
    on, exactly as typed."""

    kind: ClimbingKind
    grade: str = Field(min_length=1)


# A reading's option copy, in `components/climbing_copy.html`, is this plus its kind.
_READING_COPY_KEY = "climbing_reading_"


class ClimbingQuestion(ExtractionQuestion):
    readings: list[ClimbingReading] = Field(min_length=2)

    @field_validator("readings")
    @classmethod
    def _one_reading_per_kind(cls, readings: list[ClimbingReading]) -> list[ClimbingReading]:
        # The kind is the option's ID, which the athlete's tap sends back (ADR-0015 invariant 4).
        if len({reading.kind for reading in readings}) != len(readings):
            raise ValueError("each reading needs a kind of its own")
        return readings

    def options(self) -> tuple[ChoiceOption, ...]:
        return tuple(
            ChoiceOption(
                id=reading.kind,
                copy_key=f"{_READING_COPY_KEY}{reading.kind}",
                params={"grade": grade_in_scale(reading.grade, reading.kind)},
            )
            for reading in self.readings
        )


def recent_kinds(payloads: Sequence[Mapping[str, JsonValue]]) -> tuple[str, ...]:
    """The climbing kinds of the athlete's recent sessions, each once, from the most recent
    session first (ticket #164)."""
    kinds: dict[str, None] = {}
    for payload in payloads:
        for activity in ClimbingPayload.model_validate(payload).activities:
            kinds.setdefault(activity.kind)
    return tuple(kinds)
