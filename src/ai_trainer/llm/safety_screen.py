"""`SafetyScreenPort` on Jev (ADR-0020 use 3, G3): the `safety_screen` set's pain question."""

from uuid import UUID

from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.decisions.loader import DecisionSetLoader

SAFETY_SCREEN_ID = "safety_screen"
SAFETY_SCREEN_VERSION = 1
_PAIN = "pain"


class JevSafetyScreen:
    """Implements `SafetyScreenPort`. The state is the bare message, as the set's eval dataset
    sent it when the threshold was calibrated (#120), so no identity or profile reaches Jev.
    Only the pain question is asked: nothing reads severity yet."""

    def __init__(
        self,
        decisions: DecisionGatewayPort,
        *,
        threshold: float,
        loader: DecisionSetLoader | None = None,
    ) -> None:
        self._decisions = decisions
        self._threshold = threshold
        self._set = (loader or DecisionSetLoader()).load(
            SAFETY_SCREEN_ID, SAFETY_SCREEN_VERSION, questions={_PAIN}
        )

    async def finds_pain(self, user_id: UUID, message: str) -> bool:
        try:
            result = await self._decisions.decide(
                user_id=user_id,
                decision_id=self._set.id,
                decision_version=self._set.version,
                output_type=self._set.output_type,
                state=message,
            )
        except Exception:
            # The gateway maps model failures to outcomes; this catches the rest (its row
            # write failing, say), so the second line of defence never fails the reply.
            return False
        if result.outcome is not LlmCallOutcome.SUCCESS or result.output is None:
            return False
        pain: float = getattr(result.output, _PAIN)
        return pain >= self._threshold
