"""`picture_brief_check` on PostgreSQL (ADR-0031, ADR-0020 invariant 3, #642): Jev behind the real
decision gateway as a fake `DecisionModel`, so nothing leaves the machine. Each check writes one
`llm_calls` row under the acting user, with the set's ID and version and a NULL cost, on a
timeout too."""

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic_ai import models
from pydantic_ai.models.decision import (
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
)
from pydantic_ai.usage import RequestUsage
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.media_generation import BriefCheck
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.llm.decision_gateway import OpenRouterDecisionGateway
from ai_trainer.llm.picture_brief_check import (
    PICTURE_BRIEF_CHECK_ID,
    PICTURE_BRIEF_CHECK_VERSION,
    BriefCheckThresholds,
    JevPictureBriefCheck,
)
from ai_trainer.settings import Settings

SessionFactory = Callable[[], AsyncSession]
_INSTRUCTIONS = "Hang from a pull-up bar with your arms fully extended, then pull your chest up."
_BRIEF = "One person hanging from a bar, then chest at the bar. Plain background. No text."


class _Jev(DecisionModel[None]):
    """Answers every question with `probability`, after `delay_seconds`."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, probability: float, *, delay_seconds: float = 0) -> None:
        super().__init__()
        self._probability = probability
        self._delay_seconds = delay_seconds
        self.requests: list[DecisionRequest] = []

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
        self.requests.append(request)
        await asyncio.sleep(self._delay_seconds)
        answers: dict[str, DecisionAnswer] = {
            name: NoulAnswer(noul=self._probability) for name in request.questions
        }
        return DecisionResponse(
            answers=answers, model_name=self.model_name, usage=RequestUsage(input_tokens=150)
        )


def _refuse(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"a test reached the network: {request.url}")


async def _admin(factory: SessionFactory) -> UUID:
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=f"sub-{uuid4()}",
            email=f"zz-brief-check-{uuid4()}@example.com",
            name=None,
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    return user.id


# AC 5: each check writes an `llm_calls` row.
async def test_each_check_writes_one_llm_calls_row(
    db_session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    admin = await _admin(db_session_factory)
    calls = SqlAlchemyLlmCallsRepository(db_session_factory)
    thresholds = BriefCheckThresholds(same_movement=0.5, keeps_rules=0.5)
    settings = Settings(decision_call_timeout_seconds=0.05)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(_refuse)) as client:
        gateway = OpenRouterDecisionGateway(settings, calls, http_client=client)
        check = JevPictureBriefCheck(gateway, thresholds=thresholds)
        with gateway.agent.override(model=_Jev(0.95)):
            passed = await check.check(user_id=admin, instructions=_INSTRUCTIONS, brief=_BRIEF)
        with gateway.agent.override(model=_Jev(0.95, delay_seconds=1)):
            timed_out = await check.check(user_id=admin, instructions=_INSTRUCTIONS, brief=_BRIEF)

    assert passed == BriefCheck(passed=True, version=PICTURE_BRIEF_CHECK_VERSION)
    assert timed_out == BriefCheck(passed=False, version=PICTURE_BRIEF_CHECK_VERSION)
    rows = await calls.list_for_user(admin)
    assert sorted(row.outcome for row in rows) == sorted(
        [LlmCallOutcome.SUCCESS, LlmCallOutcome.TIMEOUT]
    )
    for row in rows:
        assert (row.user_id, row.template_id, row.template_version) == (
            admin,
            PICTURE_BRIEF_CHECK_ID,
            PICTURE_BRIEF_CHECK_VERSION,
        )
        assert row.cost is None
