"""`QuestionSportPort` on Jev (ADR-0008 amendment at #421's plan gate): the `question_sport` set
names the sport of a training question, which the router's intents don't carry."""

from collections.abc import Mapping
from uuid import UUID

from pydantic import BaseModel

from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.decisions.loader import DecisionSet, DecisionSetLoader

QUESTION_SPORT_ID = "question_sport"
QUESTION_SPORT_VERSION = 1
NO_SPORT = "none"
_SPORT = "sport"


def question_sport_set(loader: DecisionSetLoader | None = None) -> DecisionSet:
    return (loader or DecisionSetLoader()).load(QUESTION_SPORT_ID, QUESTION_SPORT_VERSION)


def confident_sport(
    output: BaseModel, probabilities: Mapping[str, Mapping[str, float]], *, threshold: float
) -> str | None:
    """The sport Jev picked, read by option name from its distribution, when it is at or above
    `threshold`. `None` for no sport or a pick below it."""
    picked: str = getattr(output, _SPORT)
    if picked == NO_SPORT or probabilities.get(_SPORT, {}).get(picked, 0.0) < threshold:
        return None
    return picked


class JevQuestionSport:
    """Implements `QuestionSportPort`. The state is the bare message, so no identity or profile
    reaches Jev (ADR-0020 data retention)."""

    def __init__(
        self,
        decisions: DecisionGatewayPort,
        *,
        threshold: float,
        loader: DecisionSetLoader | None = None,
    ) -> None:
        self._decisions = decisions
        self._threshold = threshold
        self._set = question_sport_set(loader)

    async def sport_of(self, user_id: UUID, message: str) -> str | None:
        try:
            result = await self._decisions.decide(
                user_id=user_id,
                decision_id=self._set.id,
                decision_version=self._set.version,
                output_type=self._set.output_type,
                state=message,
            )
        except Exception:
            # The gateway maps model failures to outcomes; this catches the rest (its row write
            # failing, say). No sport is the safe side: the question gets "not yet".
            return None
        if result.outcome is not LlmCallOutcome.SUCCESS or result.output is None:
            return None
        return confident_sport(result.output, result.probabilities, threshold=self._threshold)
