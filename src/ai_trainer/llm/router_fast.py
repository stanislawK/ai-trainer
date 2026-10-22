"""`FastRouterPort` on Jev (ADR-0020 use 1, ADR-0008 amendment): the `router_fast` set routes a
message only when it holds one confident intent, and a log one confident sport."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from ai_trainer.application.ports.conversation import ReplyContext
from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort
from ai_trainer.application.ports.fast_router import FastRoute
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
    `intent` and `sport`. `plausible_floor`, calibrated on the same cases, is the P(sport) at
    or above which an athlete's sport stays plausible (B23)."""

    multi_request: float
    intent: float
    sport: float
    plausible_floor: float


def router_fast_set(
    athlete_sports: Collection[str],
    *,
    loader: DecisionSetLoader,
    version: int = ROUTER_FAST_VERSION,
) -> DecisionSet:
    """The sport is asked among the athlete's own sports, and not at all when they have one
    or none: one sport is theirs without a question (B23). The app asks the active version; an
    eval may ask another (#208)."""
    if len(athlete_sports) >= 2:
        return loader.load(ROUTER_FAST_ID, version, options={_SPORT: athlete_sports})
    return loader.load(ROUTER_FAST_ID, version, questions={_MULTI_REQUEST, _INTENT})


def fast_intents(
    output: BaseModel,
    probabilities: Mapping[str, Mapping[str, float]],
    *,
    athlete_sports: Sequence[str],
    thresholds: RouterFastThresholds,
) -> list[Intent] | None:
    """The one intent Jev's answer holds, or `None` when the LLM router must decide. Choices
    are read from their distribution by option name, never from the library's pick alone."""
    kind = _confident_intent(output, probabilities, thresholds=thresholds)
    if kind is None:
        return None
    if kind != _SPORT_INTENT:
        return [Intent.model_validate({"kind": kind})]
    if len(athlete_sports) == 1:
        return [Intent(kind=_SPORT_INTENT, sport=athlete_sports[0])]
    sport: str | None = getattr(output, _SPORT, None)
    if sport is None or _picked(probabilities, _SPORT, sport) < thresholds.sport:
        return None
    return [Intent(kind=_SPORT_INTENT, sport=sport)]


def _confident_intent(
    output: BaseModel,
    probabilities: Mapping[str, Mapping[str, float]],
    *,
    thresholds: RouterFastThresholds,
) -> IntentKind | None:
    """The intent Jev picked when the message holds one request and Jev is sure of it."""
    if float(getattr(output, _MULTI_REQUEST)) > thresholds.multi_request:
        return None
    kind: IntentKind = getattr(output, _INTENT)
    if kind == _UNCLEAR or _picked(probabilities, _INTENT, kind) < thresholds.intent:
        return None
    return kind


def plausible_sports(
    probabilities: Mapping[str, Mapping[str, float]],
    athlete_sports: Sequence[str],
    *,
    floor: float,
) -> tuple[str, ...] | None:
    """The athlete's sports with P(sport) at or above `floor`, most likely first and the
    athlete's own order among equals (B23). A sport at exactly the floor counts. An athlete
    with one sport or none has no question to ask: theirs is the answer. `None` when a
    multi-sport athlete's answer holds no sport distribution."""
    if len(athlete_sports) <= 1:
        return tuple(athlete_sports)
    distribution = probabilities.get(_SPORT)
    if distribution is None:
        return None
    plausible = [sport for sport in athlete_sports if distribution.get(sport, 0.0) >= floor]
    return tuple(sorted(plausible, key=lambda sport: -distribution.get(sport, 0.0)))


def fast_route(
    output: BaseModel,
    probabilities: Mapping[str, Mapping[str, float]],
    *,
    athlete_sports: Sequence[str],
    thresholds: RouterFastThresholds,
) -> FastRoute:
    """The route and the plausible sports of one answer. The route is `fast_intents` as
    calibrated at #122. Plausible sports ride along whenever Jev is sure of one single request,
    even for a log whose sport is too split to route on: that message goes to the LLM router
    still carrying exactly the sports left in play. Several requests blur a sport
    distribution, so then the router's own sports stand in, unless the athlete has no sport
    to choose between. The confident intent rides along as `single_intent`."""
    single = _confident_intent(output, probabilities, thresholds=thresholds)
    sure_of_sport = single is not None or len(athlete_sports) <= 1
    return FastRoute(
        intents=fast_intents(
            output, probabilities, athlete_sports=athlete_sports, thresholds=thresholds
        ),
        plausible_sports=(
            plausible_sports(probabilities, athlete_sports, floor=thresholds.plausible_floor)
            if sure_of_sport
            else None
        ),
        single_intent=single,
    )


def _picked(probabilities: Mapping[str, Mapping[str, float]], question: str, option: str) -> float:
    return probabilities.get(question, {}).get(option, 0.0)


def _declined(athlete_sports: Sequence[str]) -> FastRoute:
    """Left to the LLM router. An athlete with one sport still has it as the only plausible
    one: no call is needed to know it."""
    return FastRoute(intents=None, plausible_sports=plausible_sports({}, athlete_sports, floor=1.0))


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

    async def route(self, context: ReplyContext) -> FastRoute:
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
            return _declined(sports)
        if result.outcome is not LlmCallOutcome.SUCCESS or result.output is None:
            return _declined(sports)
        return fast_route(
            result.output,
            result.probabilities,
            athlete_sports=sports,
            thresholds=self._thresholds,
        )

    def _set_for(self, sports: tuple[str, ...]) -> DecisionSet:
        if sports not in self._sets:
            self._sets[sports] = router_fast_set(sports, loader=self._loader)
        return self._sets[sports]
