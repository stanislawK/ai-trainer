from datetime import date

from pydantic import BaseModel, ConfigDict


class Goal(BaseModel):
    """What the athlete is working toward (PRD-0003 B9, ADR-0006): free text and an optional
    target date. `target_date` is a calendar date, not a moment, so it carries no timezone."""

    model_config = ConfigDict(frozen=True)

    text: str
    target_date: date | None = None
