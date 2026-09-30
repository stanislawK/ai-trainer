from datetime import date

from pydantic import BaseModel, ConfigDict


class Goal(BaseModel):
    """What the athlete is working toward (PRD-0003 B9, ADR-0006): free text, an optional
    target date and an optional sport. `target_date` is a calendar date, not a moment, so it
    carries no timezone. `sport_id` is a `SportRegistry` ID; `None` is a general goal."""

    model_config = ConfigDict(frozen=True)

    text: str
    target_date: date | None = None
    sport_id: str | None = None
