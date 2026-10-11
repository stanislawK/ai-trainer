import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
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
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
)
from pydantic_ai.usage import RequestUsage

from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.decision_gateway import (
    OPENROUTER_SYSTEM_ONE_BASE_URL,
    OpenRouterDecisionGateway,
)
from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.settings import Settings
from tests.unit.test_llm_gateway import FakeLlmCallsRepository

FIXTURES = Path(__file__).parents[1] / "fixtures" / "decisions"
# OpenRouter's documented `POST /api/v1/systemone` 200 example (openrouter.ai/openapi.json,
# `createSystemone`), with its questions renamed to the sample set's.
RECORDED_RESPONSE = FIXTURES / "openrouter_systemone_response.json"
SNAPSHOT = "typesafe/jev-1.13-20260917"
STATE = "Bouldered for two hours, my left shoulder twinged on the last problem."


class FakeJev(DecisionModel[None]):
    """A `DecisionModel` that answers the sample set without a network: the same question
    building and answer reading as `TypeSafeModel`, with only the transport replaced."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, *, delay: float = 0.0, error: Exception | None = None) -> None:
        super().__init__()
        self._delay = delay
        self._error = error
        self.requests: list[DecisionRequest] = []

    @property
    def model_name(self) -> str:
        return SNAPSHOT

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
        if self._error is not None:
            raise self._error
        answers: dict[str, DecisionAnswer] = {
            "mentions_pain": NoulAnswer(noul=0.82),
            "sport": ChoiceAnswer(
                choice="climbing",
                confidence=0.75,
                probabilities={"climbing": 0.84, "gym": 0.16, "cycling": 0.0},
            ),
            "effort": ScoreAnswer(
                score=1.99, confidence=0.99, probabilities={0: 0.0, 1: 0.01, 2: 0.99}
            ),
        }
        return DecisionResponse(
            answers=answers, model_name=SNAPSHOT, usage=RequestUsage(input_tokens=350)
        )


def _settings(timeout_seconds: float = 2.0) -> Settings:
    return Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        decision_call_timeout_seconds=timeout_seconds,
    )


def _sample() -> type[BaseModel]:
    return DecisionSetLoader(root=FIXTURES).load("sample", 1).output_type


def _refuse(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"a test reached the network: {request.url}")


@pytest.fixture(autouse=True)
def _allow_fake_decision_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DecisionModel.request` checks the no-model-requests guard even for a fake subclass, so
    it is lifted here; `_gateway` gives every real model a transport that refuses the network,
    so a lost override fails instead of calling OpenRouter (ADR-0013)."""
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _gateway(
    repository: FakeLlmCallsRepository,
    *,
    timeout_seconds: float = 2.0,
    transport: httpx2.MockTransport | None = None,
) -> OpenRouterDecisionGateway:
    return OpenRouterDecisionGateway(
        _settings(timeout_seconds),
        repository,
        http_client=httpx2.AsyncClient(transport=transport or httpx2.MockTransport(_refuse)),
    )


async def test_success_returns_output_and_probabilities_and_writes_one_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository)
    user_id = uuid4()

    with gateway.agent.override(model=FakeJev()):
        result = await gateway.decide(
            user_id=user_id,
            decision_id="sample",
            decision_version=1,
            output_type=_sample(),
            state=STATE,
        )

    assert result.outcome is LlmCallOutcome.SUCCESS
    assert result.output is not None
    assert result.output.model_dump() == {"mentions_pain": 0.82, "sport": "climbing", "effort": 2}
    assert result.probabilities == {
        "sport": {"climbing": 0.84, "gym": 0.16, "cycling": 0.0},
        "effort": {"0": 0.0, "1": 0.01, "2": 0.99},
    }
    [row] = repository.recorded
    assert row.user_id == user_id
    assert (row.template_id, row.template_version) == ("sample", 1)
    assert row.model == SNAPSHOT
    assert (row.input_tokens, row.output_tokens) == (350, 0)
    assert row.cost is None
    assert row.outcome is LlmCallOutcome.SUCCESS


async def test_loaded_questions_reach_the_model_as_probability_choice_and_score() -> None:
    fake = FakeJev()
    gateway = _gateway(FakeLlmCallsRepository())

    with gateway.agent.override(model=fake):
        await gateway.decide(
            user_id=uuid4(),
            decision_id="sample",
            decision_version=1,
            output_type=_sample(),
            state=STATE,
        )

    [request] = fake.requests
    assert request.state == STATE
    questions = request.questions
    assert isinstance(questions["mentions_pain"], NoulQuestion)
    assert isinstance(questions["sport"], ChoiceQuestion)
    assert questions["sport"].criteria == {
        "climbing": "Bouldering, sport or trad climbing, indoors or outside.",
        "gym": "Weight training or machines in a gym.",
        "cycling": "Riding a bike, on the road, off road or indoors.",
    }
    assert isinstance(questions["effort"], ScoreQuestion)
    assert questions["effort"].criteria == [
        "Easy; they could have kept going for a long time.",
        "Moderate; tiring but comfortable.",
        "Hard; close to their limit.",
    ]


async def test_timeout_gives_outcome_timeout_and_one_row_without_raising() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository, timeout_seconds=0.05)

    with gateway.agent.override(model=FakeJev(delay=5)):
        result = await gateway.decide(
            user_id=uuid4(),
            decision_id="sample",
            decision_version=1,
            output_type=_sample(),
            state=STATE,
        )

    assert result.outcome is LlmCallOutcome.TIMEOUT
    assert result.output is None
    assert result.probabilities == {}
    [row] = repository.recorded
    assert row.outcome is LlmCallOutcome.TIMEOUT
    assert row.model == "typesafe/jev-1.13"
    assert (row.input_tokens, row.output_tokens, row.cost) == (0, 0, None)
    assert (row.template_id, row.template_version) == ("sample", 1)


async def test_model_error_gives_outcome_error_and_one_row_without_raising() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository)

    with gateway.agent.override(model=FakeJev(error=RuntimeError("upstream exploded"))):
        result = await gateway.decide(
            user_id=uuid4(),
            decision_id="sample",
            decision_version=1,
            output_type=_sample(),
            state=STATE,
        )

    assert result.outcome is LlmCallOutcome.ERROR
    assert result.output is None
    [row] = repository.recorded
    assert row.outcome is LlmCallOutcome.ERROR


async def test_a_cancelled_caller_still_leaves_one_cancelled_row() -> None:
    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository, timeout_seconds=30)
    fake = FakeJev(delay=5)

    with gateway.agent.override(model=fake):
        task = asyncio.create_task(
            gateway.decide(
                user_id=uuid4(),
                decision_id="sample",
                decision_version=1,
                output_type=_sample(),
                state=STATE,
            )
        )
        async with asyncio.timeout(1):
            while not fake.requests:
                await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    [row] = repository.recorded
    assert row.outcome is LlmCallOutcome.CANCELLED


async def test_the_real_model_posts_to_openrouter_systemone_with_the_openrouter_key() -> None:
    """The unfaked `TypeSafeModel` over a mock transport that replays OpenRouter's example."""
    sent: list[httpx2.Request] = []

    def _answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json=json.loads(RECORDED_RESPONSE.read_text()))

    repository = FakeLlmCallsRepository()
    gateway = _gateway(repository, transport=httpx2.MockTransport(_answer))

    result = await gateway.decide(
        user_id=uuid4(),
        decision_id="sample",
        decision_version=1,
        output_type=_sample(),
        state=STATE,
    )

    [request] = sent
    assert str(request.url) == "https://openrouter.ai/api/v1/systemone"
    assert OPENROUTER_SYSTEM_ONE_BASE_URL == "https://openrouter.ai/api"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["model"] == "typesafe/jev-1.13"
    assert body["state"] == STATE
    assert result.outcome is LlmCallOutcome.SUCCESS
    assert result.output is not None
    assert result.output.model_dump() == {"mentions_pain": 0.82, "sport": "climbing", "effort": 2}
    [row] = repository.recorded
    assert row.model == SNAPSHOT
    assert (row.input_tokens, row.output_tokens) == (476, 70)
    # OpenRouter sends `usage.cost`, but typesafe-sdk drops it, so the row stores NULL rather
    # than a figure priced in code (ADR-0020 Consequences, ADR-0018 invariant 6).
    assert row.cost is None
