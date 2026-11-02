from typing import Protocol
from uuid import UUID


class QuestionSportPort(Protocol):
    """Which sport the athlete's training question is about (ADR-0008 amendment at #421's plan
    gate). A question intent carries no sport of its own."""

    async def sport_of(self, user_id: UUID, message: str) -> str | None:
        """The sport's registry ID when the message is confidently about one. `None` when it
        names none, the reading is unsure, or the call failed. Never raises."""
        ...


class NoQuestionSport:
    """Implements `QuestionSportPort` for a reply service that reads no sport from a question."""

    async def sport_of(self, user_id: UUID, message: str) -> str | None:
        return None


NO_QUESTION_SPORT = NoQuestionSport()
