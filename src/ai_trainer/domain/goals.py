from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class GoalKind(StrEnum):
    """What a goal is about (PRD 0013 B45, ADR-0028 #618 amendment): one of six kinds, picked by
    Jev from the goal's text (`goal_kind`) and confirmed by the athlete on the summary."""

    STRENGTH = "strength"
    MUSCLE = "muscle"
    ENDURANCE = "endurance"
    WEIGHT_LOSS = "weight_loss"
    MOBILITY = "mobility"
    GENERAL_FITNESS = "general_fitness"


class Goal(BaseModel):
    """What the athlete is working toward (PRD-0003 B9, ADR-0006): free text, an optional
    target date and an optional sport. `target_date` is a calendar date, not a moment, so it
    carries no timezone. `sport_id` is a `SportRegistry` ID; `None` is a general goal. `kind`
    is `None` for a goal no kind was placed on (B45)."""

    model_config = ConfigDict(frozen=True)

    text: str
    target_date: date | None = None
    sport_id: str | None = None
    kind: GoalKind | None = None
