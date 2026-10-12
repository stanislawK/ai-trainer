"""What `climbing.extract` returns (ADR-0008, ADR-0014): an absolute date, how it was found,
and each activity with its grade exactly as typed. Code, not the model, decides the scale."""

from datetime import date

from pydantic import BaseModel, Field, field_validator

from ai_trainer.domain.sports.base import DateSource
from ai_trainer.domain.sports.climbing.normalize import normalize_grade
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


class ClimbingExtraction(BaseModel):
    occurred_on: date
    date_source: DateSource
    activities: list[ExtractedClimb] = Field(min_length=1)

    def to_payload(self) -> ClimbingPayload:
        return ClimbingPayload(
            activities=[
                ClimbingActivity(
                    kind=climb.kind,
                    count=climb.count,
                    grade=normalize_grade(climb.grade) if climb.grade is not None else None,
                    style=climb.style,
                    attempts=climb.attempts,
                    route=climb.route,
                )
                for climb in self.activities
            ]
        )
