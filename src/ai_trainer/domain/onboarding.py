"""Onboarding answers (PRD-0007 B34, B36, ADR-0028): the athlete's prose per step, durable until
the summary is confirmed; reading it is not. A send bumps `revision`, so a read of an older
answer can tell it is stale."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue

MAX_ANSWER_LENGTH = 1000


class OnboardingStep(StrEnum):
    ABOUT_YOU = "about_you"
    GOALS = "goals"
    LEVEL = "level"
    SCHEDULE = "schedule"


class AnswerStatus(StrEnum):
    PENDING = "pending"
    PARSED = "parsed"
    FAILED = "failed"
    SKIPPED = "skipped"


class OnboardingAnswer(BaseModel):
    """One step's answer. `sport_id` is `None` for About you, which is per athlete. `parsed`
    holds what a read produced, in the step's own shape, once `status` is `parsed`."""

    model_config = ConfigDict(frozen=True)

    step: OnboardingStep
    sport_id: str | None
    text: str | None
    revision: int
    status: AnswerStatus
    parsed: dict[str, JsonValue] | None = None
