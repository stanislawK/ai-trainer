"""The Jev router fast path (ADR-0020 use 1, ADR-0008 amendment, ticket #122): `router_fast`
v1 asked of the bare message, one confident intent dispatched from its answer, and `None`, the
LLM router's turn, for everything else."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import BaseModel

from ai_trainer.application.ports.conversation import ReplyContext
from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.application.ports.fast_router import FastRoute
from ai_trainer.domain.conversation import INTENT_KINDS, Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.decisions.loader import DECISIONS_ROOT, DecisionSetLoader
from ai_trainer.llm.router_fast import (
    ROUTER_FAST_ID,
    ROUTER_FAST_VERSION,
    JevFastRouter,
    RouterFastThresholds,
    fast_intents,
    fast_route,
    plausible_sports,
    router_fast_set,
)

THRESHOLDS = RouterFastThresholds(multi_request=0.3, intent=0.8, sport=0.8, plausible_floor=0.25)
ALL_SPORTS = ("climbing", "gym", "cycling")


def _file() -> dict[str, object]:
    path = DECISIONS_ROOT / ROUTER_FAST_ID / f"v{ROUTER_FAST_VERSION}.en.yaml"
    loaded: dict[str, object] = yaml.safe_load(path.read_text())
    return loaded


def _questions() -> dict[str, dict[str, object]]:
    questions: dict[str, dict[str, object]] = _file()["questions"]  # type: ignore[assignment]
    return questions


def test_the_intent_question_offers_exactly_the_domain_intent_kinds() -> None:
    intent = _questions()["intent"]

    assert intent["kind"] == "choice"
    assert tuple(intent["options"]) == INTENT_KINDS  # type: ignore[arg-type]


def test_the_sport_question_describes_every_registered_sport() -> None:
    sport = _questions()["sport"]

    assert sport["kind"] == "choice"
    options: dict[str, str] = sport["options"]  # type: ignore[assignment]
    assert set(options) == {plugin.id for plugin in default_sport_registry().all()}


def test_several_requests_is_a_probability_question() -> None:
    assert _questions()["multi_request"]["kind"] == "probability"


def test_a_multi_sport_athlete_is_asked_the_sport_among_their_own_sports_only() -> None:
    decision_set = router_fast_set(["gym", "climbing"], loader=DecisionSetLoader())
    fields = decision_set.output_type.model_fields

    assert (decision_set.id, decision_set.version) == (ROUTER_FAST_ID, ROUTER_FAST_VERSION)
    assert list(fields) == ["multi_request", "intent", "sport"]
    decision_set.output_type.model_validate(
        {"multi_request": 0.1, "intent": "chitchat", "sport": "gym"}
    )
    with pytest.raises(ValueError, match="sport"):
        decision_set.output_type.model_validate(
            {"multi_request": 0.1, "intent": "chitchat", "sport": "cycling"}
        )


@pytest.mark.parametrize("sports", [["gym"], []])
def test_an_athlete_with_one_sport_or_none_gets_no_sport_question(sports: list[str]) -> None:
    decision_set = router_fast_set(sports, loader=DecisionSetLoader())

    assert list(decision_set.output_type.model_fields) == ["multi_request", "intent"]


def _answer(
    *,
    multi: float = 0.05,
    intent: str = "log_session",
    p_intent: float = 0.95,
    sport: str | None = "cycling",
    p_sport: float = 0.9,
    sports: tuple[str, ...] = ALL_SPORTS,
) -> tuple[BaseModel, dict[str, dict[str, float]]]:
    decision_set = router_fast_set(sports, loader=DecisionSetLoader())
    values: dict[str, object] = {"multi_request": multi, "intent": intent}
    probabilities = {"intent": {intent: p_intent}}
    if "sport" in decision_set.output_type.model_fields:
        values["sport"] = sport
        probabilities["sport"] = {str(sport): p_sport}
    return decision_set.output_type.model_validate(values), probabilities


def test_one_confident_intent_with_a_confident_sport_is_routed() -> None:
    output, probabilities = _answer()

    assert fast_intents(
        output, probabilities, athlete_sports=ALL_SPORTS, thresholds=THRESHOLDS
    ) == [Intent(kind="log_session", sport="cycling")]


def test_an_intent_other_than_a_log_carries_no_sport() -> None:
    output, probabilities = _answer(intent="ask_training_question", sport="gym", p_sport=0.1)

    assert fast_intents(
        output, probabilities, athlete_sports=ALL_SPORTS, thresholds=THRESHOLDS
    ) == [Intent(kind="ask_training_question")]


def test_a_single_sport_athletes_log_gets_their_sport_without_a_question() -> None:
    output, probabilities = _answer(sports=("gym",))

    assert fast_intents(output, probabilities, athlete_sports=("gym",), thresholds=THRESHOLDS) == [
        Intent(kind="log_session", sport="gym")
    ]


@pytest.mark.parametrize(
    ("answer", "sports"),
    [
        # "Ran 5k, also plan my week": several requests go to the LLM router (B14).
        ({"multi": 0.31}, ALL_SPORTS),
        ({"p_intent": 0.79}, ALL_SPORTS),
        ({"intent": "unclear", "p_intent": 0.99}, ALL_SPORTS),
        ({"p_sport": 0.79}, ALL_SPORTS),
        # A log with no sport to give it: the LLM router decides.
        ({}, ()),
    ],
    ids=["several requests", "unsure intent", "unclear", "unsure sport", "log without sports"],
)
def test_anything_but_one_confident_intent_falls_back(
    answer: dict[str, object], sports: tuple[str, ...]
) -> None:
    output, probabilities = _answer(sports=sports, **answer)  # type: ignore[arg-type]

    assert fast_intents(output, probabilities, athlete_sports=sports, thresholds=THRESHOLDS) is None


def test_values_at_the_thresholds_are_routed() -> None:
    output, probabilities = _answer(multi=0.3, p_intent=0.8, p_sport=0.8)

    assert fast_intents(output, probabilities, athlete_sports=ALL_SPORTS, thresholds=THRESHOLDS)


def test_a_distribution_missing_the_picked_option_falls_back() -> None:
    output, _ = _answer()

    assert fast_intents(output, {}, athlete_sports=ALL_SPORTS, thresholds=THRESHOLDS) is None


@dataclass
class _Asked:
    user_id: UUID
    decision_id: str
    decision_version: int
    output_type: type[BaseModel]
    state: str


@dataclass
class FakeDecisions:
    """Answers with `values` and `probabilities`, or fails with `outcome` or `error`."""

    values: dict[str, object] = field(
        default_factory=lambda: {"multi_request": 0.02, "intent": "chitchat", "sport": "gym"}
    )
    probabilities: dict[str, dict[str, float]] = field(
        default_factory=lambda: {"intent": {"chitchat": 0.97}, "sport": {"gym": 0.4}}
    )
    outcome: LlmCallOutcome = LlmCallOutcome.SUCCESS
    error: Exception | None = None
    asked: list[_Asked] = field(default_factory=list)

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        self.asked.append(_Asked(user_id, decision_id, decision_version, output_type, state))
        if self.error is not None:
            raise self.error
        if self.outcome is not LlmCallOutcome.SUCCESS:
            return DecisionResult(output=None, outcome=self.outcome)
        values = {
            name: value for name, value in self.values.items() if name in output_type.model_fields
        }
        return DecisionResult(
            output=output_type.model_validate(values),
            outcome=self.outcome,
            probabilities=self.probabilities,
        )


def _context(user_id: UUID, message: str, sports: tuple[str, ...] = ALL_SPORTS) -> ReplyContext:
    return ReplyContext(
        user_id=user_id,
        conversation_id=uuid4(),
        message=message,
        history=(),
        athlete_sports=sports,
        goals=(),
        now=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
        timezone="Europe/Warsaw",
    )


async def test_the_bare_message_is_asked_under_the_router_fast_id_for_the_acting_user() -> None:
    decisions = FakeDecisions()
    user_id = uuid4()

    route = await JevFastRouter(decisions, thresholds=THRESHOLDS).route(_context(user_id, "hey!"))

    assert route.intents == [Intent(kind="chitchat")]
    [asked] = decisions.asked
    assert (asked.user_id, asked.decision_id, asked.decision_version, asked.state) == (
        user_id,
        ROUTER_FAST_ID,
        ROUTER_FAST_VERSION,
        "hey!",
    )


async def test_a_single_sport_athlete_is_asked_no_sport_question() -> None:
    decisions = FakeDecisions(
        values={"multi_request": 0.02, "intent": "log_session"},
        probabilities={"intent": {"log_session": 0.95}},
    )

    route = await JevFastRouter(decisions, thresholds=THRESHOLDS).route(
        _context(uuid4(), "Did an hour, felt strong", sports=("gym",))
    )

    assert route == FastRoute(
        intents=[Intent(kind="log_session", sport="gym")], plausible_sports=("gym",)
    )
    assert "sport" not in decisions.asked[0].output_type.model_fields


async def test_several_requests_go_to_the_llm_router() -> None:
    """Confident in the intent and the sport, so only P(several requests) declines it."""
    decisions = FakeDecisions(
        values={"multi_request": 0.93, "intent": "log_session", "sport": "cycling"},
        probabilities={"intent": {"log_session": 0.99}, "sport": {"cycling": 0.99}},
    )

    assert await JevFastRouter(decisions, thresholds=THRESHOLDS).route(
        _context(uuid4(), "Ran 5k, also plan my week")
    ) == FastRoute(intents=None, plausible_sports=None)


@pytest.mark.parametrize(
    "outcome", [LlmCallOutcome.TIMEOUT, LlmCallOutcome.ERROR], ids=["timeout", "error"]
)
async def test_a_decision_that_did_not_succeed_falls_back(outcome: LlmCallOutcome) -> None:
    router = JevFastRouter(FakeDecisions(outcome=outcome), thresholds=THRESHOLDS)

    assert await router.route(_context(uuid4(), "hey!")) == FastRoute(None, None)


async def test_a_gateway_that_raises_falls_back() -> None:
    router = JevFastRouter(
        FakeDecisions(error=RuntimeError("row write failed")), thresholds=THRESHOLDS
    )

    assert await router.route(_context(uuid4(), "hey!")) == FastRoute(None, None)


async def test_an_athlete_sport_the_set_does_not_describe_falls_back_without_a_call() -> None:
    decisions = FakeDecisions()

    route = await JevFastRouter(decisions, thresholds=THRESHOLDS).route(
        _context(uuid4(), "hey!", sports=("gym", "rowing"))
    )

    assert route == FastRoute(None, None)
    assert decisions.asked == []


# Plausible sports (B23, ticket #123): the athlete's sports at or above the floor.


def test_a_sport_far_below_the_floor_is_not_plausible() -> None:
    """A board session's wording points at climbing, not gym."""
    distribution = {"sport": {"climbing": 0.93, "gym": 0.07}}

    assert plausible_sports(distribution, ("climbing", "gym"), floor=0.25) == ("climbing",)


def test_two_sports_above_the_floor_are_both_plausible_most_likely_first() -> None:
    """ "Did intervals", gym and cycling, no context."""
    distribution = {"sport": {"gym": 0.4, "cycling": 0.6}}

    assert plausible_sports(distribution, ("gym", "cycling"), floor=0.25) == ("cycling", "gym")


def test_a_sport_exactly_at_the_floor_is_plausible() -> None:
    distribution = {"sport": {"climbing": 0.75, "gym": 0.25}}

    assert plausible_sports(distribution, ("climbing", "gym"), floor=0.25) == ("climbing", "gym")


def test_equal_probabilities_keep_the_athletes_sport_order() -> None:
    distribution = {"sport": {"gym": 0.5, "cycling": 0.5}}

    assert plausible_sports(distribution, ("gym", "cycling"), floor=0.25) == ("gym", "cycling")


def test_a_sport_missing_from_the_distribution_is_not_plausible() -> None:
    assert plausible_sports({"sport": {"gym": 0.9}}, ("gym", "cycling"), floor=0.25) == ("gym",)


def test_a_floor_above_every_sport_leaves_none_plausible() -> None:
    distribution = {"sport": {"climbing": 0.4, "gym": 0.35, "cycling": 0.25}}

    assert plausible_sports(distribution, ("climbing", "gym", "cycling"), floor=0.5) == ()


@pytest.mark.parametrize("sports", [("cycling",), ("gym",)])
def test_a_single_sport_athlete_always_gets_that_sport(sports: tuple[str, ...]) -> None:
    assert plausible_sports({}, sports, floor=0.99) == sports


def test_an_athlete_with_no_sport_has_none_plausible() -> None:
    assert plausible_sports({}, (), floor=0.25) == ()


def test_a_multi_sport_athlete_without_a_sport_distribution_has_nothing_to_go_on() -> None:
    assert plausible_sports({}, ("gym", "cycling"), floor=0.25) is None


def test_a_split_sport_log_is_not_routed_but_carries_its_plausible_sports() -> None:
    """Routing is as #122 calibrated it: the LLM router decides, Jev's sports ride along."""
    output, _ = _answer(sports=("gym", "cycling"), sport="gym", p_sport=0.5)
    probabilities = {"intent": {"log_session": 0.95}, "sport": {"gym": 0.5, "cycling": 0.5}}

    route = fast_route(
        output, probabilities, athlete_sports=("gym", "cycling"), thresholds=THRESHOLDS
    )

    assert route == FastRoute(intents=None, plausible_sports=("gym", "cycling"))


def test_a_confident_log_carries_its_plausible_sports_with_its_intent() -> None:
    output, _ = _answer(sports=("climbing", "gym"), sport="climbing", p_sport=0.93)
    probabilities = {"intent": {"log_session": 0.95}, "sport": {"climbing": 0.93, "gym": 0.07}}

    route = fast_route(
        output, probabilities, athlete_sports=("climbing", "gym"), thresholds=THRESHOLDS
    )

    assert route == FastRoute(
        intents=[Intent(kind="log_session", sport="climbing")], plausible_sports=("climbing",)
    )


@pytest.mark.parametrize(
    "answer",
    [{"multi": 0.31}, {"p_intent": 0.79}, {"intent": "unclear", "p_intent": 0.99}],
    ids=["several requests", "unsure intent", "unclear"],
)
def test_without_one_confident_intent_jevs_sports_are_not_carried(
    answer: dict[str, object],
) -> None:
    """A multi-request message's sport distribution blends its requests, so the router's own
    sports stand in for it."""
    output, probabilities = _answer(sports=("gym", "cycling"), **answer)  # type: ignore[arg-type]
    probabilities["sport"] = {"gym": 0.5, "cycling": 0.5}

    route = fast_route(
        output, probabilities, athlete_sports=("gym", "cycling"), thresholds=THRESHOLDS
    )

    assert route.plausible_sports is None


async def test_the_router_carries_a_split_sport_logs_plausible_sports_without_routing_it() -> None:
    decisions = FakeDecisions(
        values={"multi_request": 0.02, "intent": "log_session", "sport": "gym"},
        probabilities={
            "intent": {"log_session": 0.97},
            "sport": {"gym": 0.55, "cycling": 0.45},
        },
    )

    route = await JevFastRouter(decisions, thresholds=THRESHOLDS).route(
        _context(uuid4(), "Did intervals", sports=("gym", "cycling"))
    )

    assert route == FastRoute(intents=None, plausible_sports=("gym", "cycling"))


@pytest.mark.parametrize(
    "answer",
    [{"multi": 0.9}, {"p_intent": 0.5}, {"intent": "unclear", "p_intent": 0.99}],
    ids=["several requests", "unsure intent", "unclear"],
)
def test_a_single_sport_athlete_keeps_their_sport_however_unsure_jev_is(
    answer: dict[str, object],
) -> None:
    output, probabilities = _answer(sports=("cycling",), **answer)  # type: ignore[arg-type]

    route = fast_route(output, probabilities, athlete_sports=("cycling",), thresholds=THRESHOLDS)

    assert route.plausible_sports == ("cycling",)


@pytest.mark.parametrize(
    "outcome", [LlmCallOutcome.TIMEOUT, LlmCallOutcome.ERROR], ids=["timeout", "error"]
)
async def test_a_single_sport_athlete_keeps_their_sport_when_jev_did_not_answer(
    outcome: LlmCallOutcome,
) -> None:
    router = JevFastRouter(FakeDecisions(outcome=outcome), thresholds=THRESHOLDS)

    route = await router.route(_context(uuid4(), "2 hours easy", sports=("cycling",)))

    assert route == FastRoute(intents=None, plausible_sports=("cycling",))


async def test_a_gateway_that_raises_still_leaves_a_single_sport_athletes_sport() -> None:
    router = JevFastRouter(FakeDecisions(error=RuntimeError("boom")), thresholds=THRESHOLDS)

    route = await router.route(_context(uuid4(), "2 hours easy", sports=("gym",)))

    assert route == FastRoute(intents=None, plausible_sports=("gym",))


def test_a_floor_of_zero_keeps_every_sport_of_the_athlete() -> None:
    assert plausible_sports({"sport": {"gym": 0.9}}, ("gym", "cycling"), floor=0.0) == (
        "gym",
        "cycling",
    )
