"""Climbing payload v1 (ADR-0006, ADR-0022). Only this model reads or writes a climbing
session's `payload`; a change bumps `CLIMBING_PAYLOAD_VERSION` and older versions are upcast
on read (ADR-0022 invariant 4)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CLIMBING_PAYLOAD_VERSION = 1

type ClimbingKind = Literal["boulder", "lead", "top_rope"]
type AscentStyle = Literal["onsight", "flash", "redpoint", "repeat", "attempt"]
type GradeScale = Literal["font", "french", "v", "yds"]


class Grade(BaseModel):
    """A grade as the athlete typed it (B20). `original` and `scale` are the truth. `french`
    and `ordinal` are derived and stay null until M5's conversion fills them on read
    (ADR-0022 Consequences, owner-directed 2026-10-12)."""

    model_config = ConfigDict(frozen=True)

    original: str
    scale: GradeScale | None
    french: str | None = None
    ordinal: int | None = None


class RouteRef(BaseModel):
    """A route the athlete named (ADR-0006 route identity). Optional, and never resolved here."""

    name: str = Field(min_length=1)
    crag: str | None = None


class ClimbingActivity(BaseModel):
    kind: ClimbingKind
    count: int = Field(ge=1)
    grade: Grade | None = None
    style: AscentStyle | None = None
    attempts: int | None = Field(default=None, ge=1)
    route: RouteRef | None = None


class ClimbingPayload(BaseModel):
    sport: Literal["climbing"] = "climbing"
    activities: list[ClimbingActivity] = Field(min_length=1)
