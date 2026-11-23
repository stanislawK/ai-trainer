"""`goal_kind` on PostgreSQL (PRD 0013 B45, ADR-0020 invariant 3, ADR-0028 #618 amendment, #625):
Jev behind the real decision gateway as a fake `DecisionModel`, so nothing leaves the machine.
The goals read asks once per goal, and each pick writes one `llm_calls` row under the athlete,
with the set's ID and version and a NULL cost; Jev's state is the goal's text only."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelResponse, TextPart, models
from pydantic_ai.models.decision import (
    ChoiceAnswer,
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingAnswer, OnboardingStep
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.decision_gateway import OpenRouterDecisionGateway
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.llm.goal_kind import GOAL_KIND_ID, GOAL_KIND_VERSION, JevGoalKind
from ai_trainer.llm.onboarding import GOALS_TEMPLATE_ID, LlmOnboardingReader
from ai_trainer.settings import Settings

SessionFactory = Callable[[], AsyncSession]
_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
_GOALS = ("Touch my toes by summer", "Deadlift 150 kg")


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 11, 23, 18, 0, tzinfo=UTC)


class _Jev(DecisionModel[None]):
    """Picks `mobility` for every goal, surely, and keeps each request's state."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self) -> None:
        super().__init__()
        self.states: list[object] = []

    @property
    def model_name(self) -> str:
        return "typesafe/jev-1.13"

    @property
    def system(self) -> str:
        return "fake"

    @property
    def base_url(self) -> str:
        return "https://jev.invalid"

    async def decide(
        self, request: DecisionRequest, model_settings: DecisionModelSettings
    ) -> DecisionResponse:
        self.states.append(request.state)
        answers: dict[str, DecisionAnswer] = {
            "kind": ChoiceAnswer(
                choice="mobility", confidence=0.95, probabilities={"mobility": 0.95}
            )
        }
        return DecisionResponse(
            answers=answers, model_name=self.model_name, usage=RequestUsage(input_tokens=120)
        )


def _read_goals(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    read = {"goals": [{"text": text} for text in _GOALS]}
    return ModelResponse(parts=[TextPart(json.dumps(read))], provider_details={"cost": 0.0001})


def _refuse(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"a test reached the network: {request.url}")


async def _athlete(factory: SessionFactory) -> UUID:
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=f"sub-{uuid4()}",
            email=f"zz-goal-kind-{uuid4()}@example.com",
            name=None,
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    return user.id


def _one_at_a_time(factory: SessionFactory) -> SessionFactory:
    """`factory`'s sessions, one open at a time: the goals read asks for its kinds concurrently,
    and the test's shared connection can't hold two savepoints that close out of order."""
    bind = factory().bind
    lock = asyncio.Lock()

    class _Serial(AsyncSession):
        async def __aenter__(self) -> Self:
            await lock.acquire()
            return await super().__aenter__()

        async def __aexit__(self, *exc: Any) -> None:
            try:
                await super().__aexit__(*exc)
            finally:
                lock.release()

    return lambda: _Serial(bind=bind, join_transaction_mode="create_savepoint")


async def test_each_pick_writes_one_llm_calls_row(
    db_session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    athlete = await _athlete(db_session_factory)
    calls = SqlAlchemyLlmCallsRepository(_one_at_a_time(db_session_factory))
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
    )
    jev = _Jev()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_refuse)) as client:
        decisions = OpenRouterDecisionGateway(settings, calls, http_client=client)
        gateway = OpenRouterGateway(settings, calls)
        reader = LlmOnboardingReader(
            registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
            gateway=gateway,
            clock=_Clock(),
            model="test/onboarding-model",
            goal_kinds=JevGoalKind(decisions, threshold=settings.goal_kind_threshold),
        )
        answer = OnboardingAnswer(
            step=OnboardingStep.GOALS,
            sport_id="gym",
            text="Touch my toes by summer and deadlift 150 kg",
            revision=1,
            status=AnswerStatus.PENDING,
        )
        with (
            decisions.agent.override(model=jev),
            gateway.agent.override(model=FunctionModel(_read_goals)),
        ):
            parsed = await reader.read(athlete, answer, sport_id="gym", timezone="Europe/Warsaw")

    assert parsed is not None
    goals = parsed["goals"]
    assert isinstance(goals, list)
    assert [goal["kind"] for goal in goals if isinstance(goal, dict)] == ["mobility", "mobility"]
    rows = await calls.list_for_user(athlete)
    picks = [row for row in rows if row.template_id == GOAL_KIND_ID]
    assert len(picks) == 2
    for row in picks:
        assert (row.user_id, row.template_version) == (athlete, GOAL_KIND_VERSION)
        assert row.cost is None
    assert [row.template_id for row in rows].count(GOALS_TEMPLATE_ID) == 1
    # The state is the goal's text only: no identity reaches Jev (ADR-0020 invariant 9).
    assert sorted((json.loads(str(state)) for state in jev.states), key=str) == sorted(
        ({"goal": text} for text in _GOALS), key=str
    )
    assert all(str(athlete) not in str(state) for state in jev.states)
