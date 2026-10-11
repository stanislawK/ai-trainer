"""`FastRouterPort` on Jev (ADR-0020 use 1, ADR-0008 amendment): the `router_fast` set routes a
message only when it holds one confident intent, and a log one confident sport."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from ai_trainer.application.ports.conversation import ReplyContext
from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort
from ai_trainer.domain.conversation import Intent, IntentKind
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.decisions.loader import DecisionSet, DecisionSetLoader

ROUTER_FAST_ID = "router_fast"
ROUTER_FAST_VERSION = 1
_MULTI_REQUEST = "multi_request"
_INTENT = "intent"
_SPORT = "sport"
_UNCLEAR: IntentKind = "unclear"
_SPORT_INTENT: IntentKind = "log_session"


@dataclass(frozen=True, slots=True)
class RouterFastThresholds:
    """From `Settings`, calibrated on the router dataset: P(several requests) at or below
    `multi_request`, and the picked intent's and a log's sport's probability at or above
    `intent` and `sport`."""

    multi_request: float
    intent: float
    sport: float


def router_fast_set(athlete_sports: Collection[str], *, loader: DecisionSetLoader) -> DecisionSet:
    """The sport is asked among the athlete's own sports, and not at all when they have one
    or none: one sport is theirs without a question (B23)."""
    if len(athlete_sports) >= 2:
        return loader.load(ROUTER_FAST_ID, ROUTER_FAST_VERSION, options={_SPORT: athlete_sports})
    return loader.load(ROUTER_FAST_ID, ROUTER_FAST_VERSION, questions={_MULTI_REQUEST, _INTENT})


def fast_intents(
    output: BaseModel,
    probabilities: Mapping[str, Mapping[str, float]],
    *,
    athlete_sports: Sequence[str],
    thresholds: RouterFastThresholds,
) -> list[Intent] | None:
    """The one intent Jev's answer holds, or `None` when the LLM router must decide. Choices
    are read from their distribution by option name, never from the library's pick alone."""
    if float(getattr(output, _MULTI_REQUEST)) > thresholds.multi_request:
        return None
    kind: str = getattr(output, _INTENT)
    if kind == _UNCLEAR or _picked(probabilities, _INTENT, kind) < thresholds.intent:
        return None
    if kind != _SPORT_INTENT:
        return [Intent.model_validate({"kind": kind})]
    if len(athlete_sports) == 1:
        return [Intent(kind=_SPORT_INTENT, sport=athlete_sports[0])]
    sport: str | None = getattr(output, _SPORT, None)
    if sport is None or _picked(probabilities, _SPORT, sport) < thresholds.sport:
        return None
    return [Intent(kind=_SPORT_INTENT, sport=sport)]


def _picked(probabilities: Mapping[str, Mapping[str, float]], question: str, option: str) -> float:
    return probabilities.get(question, {}).get(option, 0.0)


class JevFastRouter:
    """Implements `FastRouterPort`. The state is the bare message, as the router dataset sent
    it when the thresholds were calibrated, so no identity, history or profile reaches Jev
    (ADR-0020 data retention)."""

    def __init__(
        self,
        decisions: DecisionGatewayPort,
        *,
        thresholds: RouterFastThresholds,
        loader: DecisionSetLoader | None = None,
    ) -> None:
        self._decisions = decisions
        self._thresholds = thresholds
        self._loader = loader or DecisionSetLoader()
        self._sets: dict[tuple[str, ...], DecisionSet] = {}

    async def route(self, context: ReplyContext) -> list[Intent] | None:
        sports = tuple(context.athlete_sports)
        try:
            decision_set = self._set_for(sports)
            result = await self._decisions.decide(
                user_id=context.user_id,
                decision_id=decision_set.id,
                decision_version=decision_set.version,
                output_type=decision_set.output_type,
                state=context.message,
            )
        except Exception:
            # A sport the set does not describe, or a failure the gateway did not map to an
            # outcome (its row write, say): the LLM router still answers.
            return None
        if result.outcome is not LlmCallOutcome.SUCCESS or result.output is None:
            return None
        return fast_intents(
            result.output,
            result.probabilities,
            athlete_sports=sports,
            thresholds=self._thresholds,
        )

    def _set_for(self, sports: tuple[str, ...]) -> DecisionSet:
        if sports not in self._sets:
            self._sets[sports] = router_fast_set(sports, loader=self._loader)
        return self._sets[sports]
