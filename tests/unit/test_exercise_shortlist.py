"""Jev picks the exercise from the shortlist (ADR-0031, ADR-0020, ticket #399): one `choice`
question over this log's candidates and "none of these", asked of the typed name alone, and a
pick that counts only above the `Settings` threshold. Everything else is the card's."""

import asyncio
from uuid import UUID, uuid4

import httpx2
import pytest
import yaml
from pydantic import BaseModel, PostgresDsn, SecretStr
from pydantic_ai import models
from pydantic_ai.models.decision import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
)
from pydantic_ai.usage import RequestUsage

from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.decision_gateway import OpenRouterDecisionGateway
from ai_trainer.llm.decisions.loader import DECISIONS_ROOT
from ai_trainer.llm.exercise_shortlist import (
    EXERCISE_SHORTLIST_ID,
    EXERCISE_SHORTLIST_VERSION,
    JevExercisePicker,
    confident_pick,
    exercise_shortlist_set,
)
from ai_trainer.settings import Settings
from tests.unit.test_llm_gateway import FakeLlmCallsRepository

_BARBELL = ExerciseCandidate(
    id=uuid4(), name="Barbell Bench Press", equipment="barbell", target="pectorals"
)
_DUMBBELL = ExerciseCandidate(id=uuid4(), name="Dumbbell Bench Press", equipment="dumbbell")
_THRESHOLD = 0.9


class FakeDecisions:
    """A `DecisionGatewayPort` that answers one option with one probability, or fails."""

    def __init__(
        self,
        option: str | None = None,
        probability: float = 0.97,
        outcome: LlmCallOutcome = LlmCallOutcome.SUCCESS,
        error: Exception | None = None,
    ) -> None:
        self.option = option
        self.probability = probability
        self.outcome = outcome
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        self.calls.append(
            {
                "user_id": user_id,
                "decision_id": decision_id,
                "decision_version": decision_version,
                "state": state,
                "options": [
                    option["const"]
                    for option in output_type.model_json_schema()["properties"]["exercise"]["anyOf"]
                ],
            }
        )
        if self.error is not None:
            raise self.error
        if self.outcome is not LlmCallOutcome.SUCCESS or self.option is None:
            return DecisionResult(output=None, outcome=self.outcome)
        return DecisionResult(
            output=output_type.model_validate({"exercise": self.option}),
            outcome=LlmCallOutcome.SUCCESS,
            probabilities={"exercise": {self.option: self.probability}},
        )


def _picker(decisions: FakeDecisions) -> JevExercisePicker:
    return JevExercisePicker(decisions, threshold=_THRESHOLD)


async def test_a_pick_above_the_threshold_is_that_candidate() -> None:
    decisions = FakeDecisions(str(_DUMBBELL.id), 0.95)

    picked = await _picker(decisions).pick(uuid4(), "db bench", [_BARBELL, _DUMBBELL])

    assert picked == _DUMBBELL


async def test_a_pick_at_the_threshold_counts() -> None:
    picked = await _picker(FakeDecisions(str(_BARBELL.id), _THRESHOLD)).pick(
        uuid4(), "bench press", [_BARBELL, _DUMBBELL]
    )

    assert picked == _BARBELL


async def test_a_pick_below_the_threshold_returns_no_pick() -> None:
    decisions = FakeDecisions(str(_BARBELL.id), 0.89)

    assert await _picker(decisions).pick(uuid4(), "bench press", [_BARBELL, _DUMBBELL]) is None


async def test_none_of_these_returns_no_pick_however_sure_it_is() -> None:
    decisions = FakeDecisions("none", 0.99)

    assert await _picker(decisions).pick(uuid4(), "rising arms", [_BARBELL, _DUMBBELL]) is None


@pytest.mark.parametrize(
    "decisions",
    [
        FakeDecisions(outcome=LlmCallOutcome.TIMEOUT),
        FakeDecisions(outcome=LlmCallOutcome.ERROR),
        FakeDecisions(error=RuntimeError("the call row could not be written")),
    ],
    ids=["timeout", "error", "gateway_raises"],
)
async def test_a_timeout_or_an_error_returns_no_pick(decisions: FakeDecisions) -> None:
    assert await _picker(decisions).pick(uuid4(), "bench press", [_BARBELL, _DUMBBELL]) is None


async def test_a_failure_the_gateway_did_not_map_is_logged_by_type_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    decisions = FakeDecisions(error=RuntimeError("bench press for 123e4567"))

    with caplog.at_level("WARNING", logger="ai_trainer.llm.exercise_shortlist"):
        assert await _picker(decisions).pick(uuid4(), "bench press", [_BARBELL, _DUMBBELL]) is None

    assert [record.getMessage() for record in caplog.records] == [
        "exercise pick failed: RuntimeError"
    ]


async def test_an_option_that_is_no_candidate_returns_no_pick() -> None:
    decisions = FakeDecisions(str(uuid4()), 0.99)

    assert await _picker(decisions).pick(uuid4(), "bench press", [_BARBELL, _DUMBBELL]) is None


async def test_an_empty_shortlist_makes_no_call() -> None:
    decisions = FakeDecisions("none")

    assert await _picker(decisions).pick(uuid4(), "bench press", []) is None
    assert decisions.calls == []


async def test_the_state_is_the_typed_name_only() -> None:
    decisions = FakeDecisions("none")
    user_id = uuid4()

    await _picker(decisions).pick(user_id, "  bench press 60kg ", [_BARBELL, _DUMBBELL])

    [call] = decisions.calls
    assert call["state"] == "bench press 60kg"
    assert str(user_id) not in str(call["state"])
    assert (call["decision_id"], call["decision_version"]) == (
        EXERCISE_SHORTLIST_ID,
        EXERCISE_SHORTLIST_VERSION,
    )


async def test_the_candidates_are_the_options_with_none_last() -> None:
    decisions = FakeDecisions("none")

    await _picker(decisions).pick(uuid4(), "bench press", [_BARBELL, _DUMBBELL])

    [call] = decisions.calls
    assert call["options"] == [str(_BARBELL.id), str(_DUMBBELL.id), "none"]


def test_each_candidate_is_described_by_its_name_equipment_and_target() -> None:
    decision_set = exercise_shortlist_set([_BARBELL, _DUMBBELL])
    prop = decision_set.output_type.model_json_schema()["properties"]["exercise"]

    assert {option["const"]: option["description"] for option in prop["anyOf"]} == {
        str(_BARBELL.id): "Barbell Bench Press (barbell, pectorals)",
        str(_DUMBBELL.id): "Dumbbell Bench Press (dumbbell)",
        "none": yaml.safe_load(
            (
                DECISIONS_ROOT / EXERCISE_SHORTLIST_ID / f"v{EXERCISE_SHORTLIST_VERSION}.en.yaml"
            ).read_text()
        )["questions"]["exercise"]["options"]["none"],
    }


def test_a_candidate_with_no_detail_is_described_by_its_name() -> None:
    bare = ExerciseCandidate(id=uuid4(), name="Plank")
    prop = exercise_shortlist_set([bare, _BARBELL]).output_type.model_json_schema()["properties"]

    assert {o["const"]: o["description"] for o in prop["exercise"]["anyOf"]}[
        str(bare.id)
    ] == "Plank"


def test_confident_pick_reads_the_picked_options_probability_by_name() -> None:
    output = exercise_shortlist_set([_BARBELL, _DUMBBELL]).output_type.model_validate(
        {"exercise": str(_BARBELL.id)}
    )
    picked = {"exercise": {str(_BARBELL.id): 0.91, str(_DUMBBELL.id): 0.09}}

    assert confident_pick(output, picked, threshold=0.9) == str(_BARBELL.id)
    assert confident_pick(output, picked, threshold=0.92) is None
    assert confident_pick(output, {}, threshold=0.5) is None


# --- accounting: the real gateway, a fake Jev, no network ---------------------------------------


class _FakeJev(DecisionModel[None]):
    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, choice: str, *, delay: float = 0.0) -> None:
        super().__init__()
        self._choice = choice
        self._delay = delay
        self.requests: list[DecisionRequest] = []

    @property
    def model_name(self) -> str:
        return "typesafe/jev-1.13-20260917"

    @property
    def system(self) -> str:
        return "fake"

    @property
    def base_url(self) -> str:
        return "https://jev.invalid"

    async def decide(
        self, request: DecisionRequest, model_settings: DecisionModelSettings
    ) -> DecisionResponse:
        self.requests.append(request)
        if self._delay:
            await asyncio.sleep(self._delay)
        answers: dict[str, DecisionAnswer] = {
            "exercise": ChoiceAnswer(
                choice=self._choice,
                confidence=0.97,
                probabilities={self._choice: 0.97, "none": 0.03},
            )
        }
        return DecisionResponse(
            answers=answers,
            model_name=self.model_name,
            usage=RequestUsage(input_tokens=210),
        )


def _real_gateway(repository: FakeLlmCallsRepository) -> OpenRouterDecisionGateway:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"a test reached the network: {request.url}")

    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        decision_call_timeout_seconds=0.05,
    )
    return OpenRouterDecisionGateway(
        settings, repository, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
    )


@pytest.fixture
def _allow_fake_decision_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


@pytest.mark.usefixtures("_allow_fake_decision_requests")
async def test_each_call_writes_one_llm_calls_row_with_the_set_and_version() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _real_gateway(repository)
    jev = _FakeJev(str(_BARBELL.id))
    user_id = uuid4()

    with gateway.agent.override(model=jev):
        picked = await JevExercisePicker(gateway, threshold=_THRESHOLD).pick(
            user_id, "bench press", [_BARBELL, _DUMBBELL]
        )

    assert picked == _BARBELL
    [row] = repository.recorded
    assert row.user_id == user_id
    assert (row.template_id, row.template_version) == (
        EXERCISE_SHORTLIST_ID,
        EXERCISE_SHORTLIST_VERSION,
    )
    assert row.cost is None
    assert row.outcome is LlmCallOutcome.SUCCESS
    [request] = jev.requests
    assert request.state == "bench press"
    assert isinstance(request.questions["exercise"], ChoiceQuestion)


@pytest.mark.usefixtures("_allow_fake_decision_requests")
async def test_a_timeout_writes_its_row_and_returns_no_pick() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _real_gateway(repository)

    with gateway.agent.override(model=_FakeJev(str(_BARBELL.id), delay=5)):
        picked = await JevExercisePicker(gateway, threshold=_THRESHOLD).pick(
            uuid4(), "bench press", [_BARBELL, _DUMBBELL]
        )

    assert picked is None
    [row] = repository.recorded
    assert row.outcome is LlmCallOutcome.TIMEOUT
