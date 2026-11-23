"""`GoalKindPort` on Jev (PRD 0013 B45, ADR-0028 #618 amendment): the `goal_kind` set picks one
of six kinds for an onboarding goal's text. `none`, a pick below the threshold, or any failure is
no kind, and onboarding goes on."""

from collections.abc import Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort
from ai_trainer.domain.goals import GoalKind
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.decisions.loader import DecisionSet, DecisionSetLoader

GOAL_KIND_ID = "goal_kind"
GOAL_KIND_VERSION = 1
NO_KIND = "none"
_KIND = "kind"


class GoalKindState(BaseModel):
    """What Jev reads: the goal's text only. Extra fields are refused, so no identity can ride
    along (ADR-0020 invariant 9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str


def goal_kind_set(loader: DecisionSetLoader | None = None) -> DecisionSet:
    return (loader or DecisionSetLoader()).load(GOAL_KIND_ID, GOAL_KIND_VERSION)


def confident_kind(
    output: BaseModel, probabilities: Mapping[str, Mapping[str, float]], *, threshold: float
) -> GoalKind | None:
    """The kind Jev picked, read by option name from its distribution, when it is at or above
    `threshold`. `None` for `none` or a pick below it."""
    picked: str = getattr(output, _KIND)
    if picked == NO_KIND or probabilities.get(_KIND, {}).get(picked, 0.0) < threshold:
        return None
    return GoalKind(picked)


class JevGoalKind:
    """Implements `GoalKindPort`."""

    def __init__(
        self,
        decisions: DecisionGatewayPort,
        *,
        threshold: float,
        loader: DecisionSetLoader | None = None,
    ) -> None:
        self._decisions = decisions
        self._threshold = threshold
        self._set = goal_kind_set(loader)

    async def kind_of(self, user_id: UUID, goal: str) -> GoalKind | None:
        try:
            result = await self._decisions.decide(
                user_id=user_id,
                decision_id=self._set.id,
                decision_version=self._set.version,
                output_type=self._set.output_type,
                state=GoalKindState(goal=goal).model_dump_json(),
            )
        except Exception:
            # The gateway maps model failures to outcomes; this catches the rest (its row write
            # failing, say). No kind is the safe side: the athlete picks one on the summary.
            return None
        if result.outcome is not LlmCallOutcome.SUCCESS or result.output is None:
            return None
        return confident_kind(result.output, result.probabilities, threshold=self._threshold)
