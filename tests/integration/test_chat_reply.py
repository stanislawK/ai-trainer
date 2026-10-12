"""The streamed reply end to end on PostgreSQL (tickets #82, #121 and #122): the real gateways,
repositories, prompt templates and decision sets, with OpenRouter replaced by a scripted
`FunctionModel` (ADR-0007) and Jev by a fake `DecisionModel` (ADR-0020). Every model and
decision call leaves its `llm_calls` row (ADR-0018), and a reply survives a reload."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models import decision as decision_models
from pydantic_ai.models.decision import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.reply import ReplyServices
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import INTENT_KINDS
from ai_trainer.domain.llm_calls import LlmCall, LlmCallOutcome, NewLlmCall
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.llm.conversation import LlmConversation, build_prompt_registry
from ai_trainer.llm.decision_gateway import OpenRouterDecisionGateway
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.llm.router_fast import JevFastRouter, RouterFastThresholds
from ai_trainer.llm.safety_screen import JevSafetyScreen
from ai_trainer.settings import Settings
from ai_trainer.web.chat import build_chat_router
from ai_trainer.web.templating import build_templates
from tests.chat_support import FROZEN_NOW, FixedClock, sign_in_as

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
_HTMX = {"HX-Request": "true"}
# The router's answer for each message the athlete sends, as the model would write it.
_ROUTED = {
    "hi!": '{"intents": [{"kind": "chitchat", "confidence": 0.9, "span": "hi!"}]}',
    "log my ride": (
        '{"intents": [{"kind": "log_session", "sport": "cycling", "confidence": 0.9,'
        ' "span": "log my ride"}]}'
    ),
    "my knee hurts, also hi": (
        '{"intents": [{"kind": "chitchat", "confidence": 0.8, "span": "also hi"},'
        ' {"kind": "wellbeing_or_injury", "confidence": 0.95, "span": "my knee hurts"}]}'
    ),
    # Router misses: the pain is there, but no `wellbeing_or_injury` intent (#121).
    "knee's a bit sore lol, anyway hi": (
        '{"intents": [{"kind": "chitchat", "confidence": 0.9,'
        ' "span": "knee\'s a bit sore lol, anyway hi"}]}'
    ),
    "Ran 5k, also plan my week": (
        '{"intents": [{"kind": "log_session", "sport": "cycling", "confidence": 0.6,'
        ' "span": "Ran 5k"}, {"kind": "request_plan", "confidence": 0.9,'
        ' "span": "also plan my week"}]}'
    ),
    "knee's sore after bouldering, log it": (
        '{"intents": [{"kind": "log_session", "sport": "climbing", "confidence": 0.9,'
        ' "span": "knee\'s sore after bouldering, log it"}]}'
    ),
}
_JEV_SNAPSHOT = "typesafe/jev-1.13-20260917"


# `router_fast`'s answers to a message it routes itself: P(several requests), then each
# choice's pick and its probability (ticket #122).
type FastAnswer = tuple[float, dict[str, tuple[str, float]]]
_DECLINED: FastAnswer = (0.93, {"intent": ("log_session", 0.55), "sport": ("cycling", 0.5)})


class FakeJev(DecisionModel[None]):
    """Answers `safety_screen`'s pain question as the #120 baseline did (0.97 for a sore knee,
    0.04 for a greeting), and `router_fast` from `fast` per message, declining any other as
    several requests; after `delay` seconds. Only the transport is replaced."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, *, delay: float = 0.0, fast: dict[str, FastAnswer] | None = None) -> None:
        super().__init__()
        self._delay = delay
        self._fast = fast or {}
        self.states: list[object] = []
        self.fast_asked: list[dict[str, object]] = []

    @property
    def model_name(self) -> str:
        return _JEV_SNAPSHOT

    @property
    def system(self) -> str:
        return "fake"

    @property
    def base_url(self) -> str:
        return "https://jev.invalid"

    async def decide(
        self, request: DecisionRequest, model_settings: DecisionModelSettings
    ) -> DecisionResponse:
        await asyncio.sleep(self._delay)
        if "pain" not in request.questions:
            return self._route(request)
        self.states.append(request.state)
        pain = 0.97 if "knee" in str(request.state) else 0.04
        answers: dict[str, DecisionAnswer] = {"pain": NoulAnswer(noul=pain)}
        return DecisionResponse(
            answers=answers, model_name=_JEV_SNAPSHOT, usage=RequestUsage(input_tokens=120)
        )

    def _route(self, request: DecisionRequest) -> DecisionResponse:
        offered = {
            name: list(question.criteria)
            for name, question in request.questions.items()
            if isinstance(question, ChoiceQuestion)
        }
        self.fast_asked.append({"state": request.state, **offered})
        multi, picks = self._fast.get(str(request.state), _DECLINED)
        answers: dict[str, DecisionAnswer] = {"multi_request": NoulAnswer(noul=multi)}
        for name, options in offered.items():
            pick, p = picks[name]
            rest = (1 - p) / (len(options) - 1)
            answers[name] = ChoiceAnswer(
                choice=pick,
                confidence=p,
                probabilities={option: p if option == pick else rest for option in options},
            )
        return DecisionResponse(
            answers=answers, model_name=_JEV_SNAPSHOT, usage=RequestUsage(input_tokens=120)
        )


@pytest.fixture(autouse=True)
def _allow_fake_decision_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DecisionModel.request` checks the no-model-requests guard even for a fake subclass, so
    it is lifted for decision models only; chat models stay guarded, and the decision gateway's
    transport refuses the network, so a lost override fails instead (ADR-0013)."""
    monkeypatch.setattr(decision_models, "check_allow_model_requests", lambda: None)


def _refuse(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"a test reached the network: {request.url}")


class _OneWriteAtATime:
    """The router's and the screen's rows are written concurrently. In the app each session
    checks out its own pooled connection; here every session joins the test's one connection
    through SAVEPOINTs, which interleaved writes would tangle, so this test serializes them.
    The test client may serve each request on its own event loop, so each loop gets a lock."""

    def __init__(self, calls: SqlAlchemyLlmCallsRepository) -> None:
        self._calls = calls
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    async def record(self, call: NewLlmCall) -> LlmCall:
        async with self._locks.setdefault(asyncio.get_running_loop(), asyncio.Lock()):
            return await self._calls.record(call)

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        return await self._calls.list_for_user(user_id)


def _prompt(messages: list[ModelMessage]) -> str:
    request = messages[-1]
    assert isinstance(request, ModelRequest)
    part = request.parts[-1]
    assert isinstance(part, UserPromptPart)
    assert isinstance(part.content, str)
    return part.content


def _router(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart(_ROUTED[_prompt(messages)])], provider_details={"cost": 0.0001}
    )


class _RecordingRouter:
    """`_router`, keeping every prompt it was given."""

    def __init__(self) -> None:
        self.prompts: list[list[ModelMessage]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.prompts.append(messages)
        return _router(messages, info)


class ScriptedChitchat:
    """Streams a reply; the first `hangs` calls never answer, so the gateway times out."""

    def __init__(self, hangs: int = 0) -> None:
        self.hangs = hangs
        self.instructions: list[str] = []
        self.prompts: list[list[ModelMessage]] = []

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        self.instructions.append(str(request.instructions))
        self.prompts.append(messages)
        if self.hangs:
            self.hangs -= 1
            await asyncio.sleep(10)
        for chunk in ("Sorry about the knee. ", "Hi!") if "knee" in _prompt(messages) else ("Hi!",):
            yield chunk


class _ModelOverride:
    """Runs every request, streamed body included, under `agent.override`, which is a
    ContextVar: the test client serves the app on its own thread, outside the test's context."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        gateways: tuple[OpenRouterGateway, OpenRouterDecisionGateway],
        model: Model,
        jev: FakeJev | None = None,
    ) -> None:
        self.app, self.gateways, self.model, self.jev = app, gateways, model, jev or FakeJev()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        gateway, decisions = self.gateways
        with gateway.agent.override(model=self.model), decisions.agent.override(model=self.jev):
            await self.app(scope, receive, send)


async def _user(factory: Callable[[], AsyncSession], sub: str) -> User:
    return await SqlAlchemyUsersRepository(factory).create(
        NewUser(sub=sub, email=f"{sub}@x.io", name="Ada", locale="en", status=UserStatus.ACTIVE)
    )


def _app(
    factory: Callable[[], AsyncSession],
    user: User,
    *,
    timeout: float = 30.0,
    decision_timeout: float = 30.0,
) -> tuple[FastAPI, tuple[OpenRouterGateway, OpenRouterDecisionGateway]]:
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        llm_call_timeout_seconds=timeout,
        decision_call_timeout_seconds=decision_timeout,
    )
    llm_calls = _OneWriteAtATime(SqlAlchemyLlmCallsRepository(factory))
    gateway = OpenRouterGateway(settings, llm_calls)
    decisions = OpenRouterDecisionGateway(
        settings,
        llm_calls,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(_refuse)),
    )
    chat = SqlAlchemyChatRepository(factory)
    conversation = LlmConversation(
        registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
        gateway=gateway,
        sports=default_sport_registry(),
        router_model=settings.router_model,
        chitchat_model=settings.chitchat_model,
        history_turns=settings.chat_history_turns,
    )
    replies = ReplyServices(
        chat=chat,
        conversations=SqlAlchemyConversationRepository(factory),
        profile=SqlAlchemyOnboardingRepository(factory),
        conversation=conversation,
        fast_router=JevFastRouter(
            decisions,
            thresholds=RouterFastThresholds(
                multi_request=settings.router_fast_multi_request_threshold,
                intent=settings.router_fast_intent_threshold,
                sport=settings.router_fast_sport_threshold,
                plausible_floor=settings.router_fast_plausible_sport_floor,
            ),
        ),
        safety_screen=JevSafetyScreen(decisions, threshold=settings.safety_screen_pain_threshold),
        clock=FixedClock(),
        history_turns=settings.chat_history_turns,
    )
    app = FastAPI()
    app.include_router(
        build_chat_router(
            build_templates(), chat=chat, replies=replies, sports=default_sport_registry()
        )
    )
    sign_in_as(app, user)
    return app, (gateway, decisions)


async def _thread(factory: Callable[[], AsyncSession], user_id: UUID) -> UUID:
    """The user's latest conversation, the one `/` opens; started now if they have none."""
    conversations = SqlAlchemyConversationRepository(factory)
    recent = await conversations.list_recent(user_id, 1)
    if recent:
        return recent[0].id
    return (await conversations.create(user_id, "A chat", FROZEN_NOW)).id


async def _history(
    factory: Callable[[], AsyncSession], user_id: UUID, limit: int = 50
) -> list[ChatMessage]:
    """The latest messages of the user's latest conversation, oldest first."""
    thread = await _thread(factory, user_id)
    return list(await SqlAlchemyChatRepository(factory).list_recent(user_id, thread, limit))


async def _send_and_stream(
    factory: Callable[[], AsyncSession],
    user: User,
    text: str,
    chitchat: ScriptedChitchat,
    *,
    timeout: float = 30.0,
    streams: int = 1,
    jev: FakeJev | None = None,
    decision_timeout: float = 30.0,
    router: Callable[[list[ModelMessage], AgentInfo], ModelResponse] = _router,
) -> tuple[list[str], str]:
    """Sends `text`, reads its reply stream `streams` times (a retry after the first) and
    reloads the page."""
    app, gateways = _app(factory, user, timeout=timeout, decision_timeout=decision_timeout)
    model = FunctionModel(router, stream_function=chitchat)
    app.add_middleware(_ModelOverride, gateways=gateways, model=model, jev=jev)
    client = TestClient(app)
    # Posted from the composer of `/`, which shows the latest conversation.
    thread = await _thread(factory, user.id)
    client.post("/messages", data={"message": text, "conversation_id": str(thread)}, headers=_HTMX)
    [asked] = await _history(factory, user.id, 1)
    bodies = [client.get(f"/messages/{asked.id}/reply").text for _ in range(streams)]
    return bodies, client.get("/").text


async def test_hi_streams_a_reply_that_survives_a_reload_with_a_row_per_model_call(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "reply-a")

    [body], page = await _send_and_stream(db_session_factory, user, "hi!", ScriptedChitchat())

    assert "Hi!" in body
    assert "Hi!" in page
    messages = await _history(db_session_factory, user.id)
    assert [(m.role, m.template_id) for m in messages] == [
        (ChatRole.USER, None),
        (ChatRole.ASSISTANT, "chitchat"),
    ]
    calls = await SqlAlchemyLlmCallsRepository(db_session_factory).list_for_user(user.id)
    assert sorted((call.template_id, call.outcome) for call in calls) == [
        ("chitchat", LlmCallOutcome.SUCCESS),
        ("router", LlmCallOutcome.SUCCESS),
        ("router_fast", LlmCallOutcome.SUCCESS),
        ("safety_screen", LlmCallOutcome.SUCCESS),
    ]


async def test_log_my_ride_gets_the_not_yet_reply_and_only_the_router_row(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "reply-b")
    chitchat = ScriptedChitchat()

    await _send_and_stream(db_session_factory, user, "log my ride", chitchat)

    assert chitchat.instructions == []
    [_, reply] = await _history(db_session_factory, user.id)
    assert (reply.template_id, reply.template_version, reply.sports) == ("not_yet", 1, ("cycling",))
    calls = await SqlAlchemyLlmCallsRepository(db_session_factory).list_for_user(user.id)
    assert sorted(call.template_id for call in calls) == ["router", "router_fast", "safety_screen"]


async def test_my_knee_hurts_also_hi_puts_the_pain_first_in_one_chitchat_call(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "reply-c")
    chitchat = ScriptedChitchat()

    [body], _ = await _send_and_stream(db_session_factory, user, "my knee hurts, also hi", chitchat)

    [instructions] = chitchat.instructions
    assert "Pain or feeling unwell comes first" in instructions
    assert "my knee hurts, also hi" in instructions
    assert body.index("Sorry about the knee.") < body.index("Hi!")


async def test_a_timeout_leaves_a_timeout_row_and_the_retry_streams_a_fresh_reply(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "reply-d")

    [failed, retried], page = await _send_and_stream(
        db_session_factory, user, "hi!", ScriptedChitchat(hangs=1), timeout=0.2, streams=2
    )

    assert "took too long" in failed
    assert "Hi!" in retried
    assert "Hi!" in page
    calls = await SqlAlchemyLlmCallsRepository(db_session_factory).list_for_user(user.id)
    assert sorted((call.template_id, call.outcome) for call in calls) == [
        ("chitchat", LlmCallOutcome.SUCCESS),
        ("chitchat", LlmCallOutcome.TIMEOUT),
        ("router", LlmCallOutcome.SUCCESS),
        ("router", LlmCallOutcome.SUCCESS),
        ("router_fast", LlmCallOutcome.SUCCESS),
        ("router_fast", LlmCallOutcome.SUCCESS),
        ("safety_screen", LlmCallOutcome.SUCCESS),
        ("safety_screen", LlmCallOutcome.SUCCESS),
    ]


async def test_another_users_message_is_404_for_its_reply_and_retry_with_no_model_call(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    owner = await _user(db_session_factory, "reply-e")
    stranger = await _user(db_session_factory, "reply-f")
    theirs = await SqlAlchemyChatRepository(db_session_factory).add(
        owner.id, await _thread(db_session_factory, owner.id), ChatRole.USER, "hi!", FROZEN_NOW
    )
    app, gateways = _app(db_session_factory, stranger)
    app.add_middleware(
        _ModelOverride,
        gateways=gateways,
        model=FunctionModel(_router, stream_function=ScriptedChitchat()),
    )
    client = TestClient(app)

    assert client.get(f"/messages/{theirs.id}/reply").status_code == 404
    assert client.get(f"/messages/{theirs.id}/retry", headers=_HTMX).status_code == 404
    for user in (owner, stranger):
        assert await SqlAlchemyLlmCallsRepository(db_session_factory).list_for_user(user.id) == []


async def test_a_reply_finishing_after_the_next_message_stays_with_its_own_message(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Send A, then B before A's reply came through (both before the clock's now): A's reply
    belongs to A, and B gets its own reply rather than replaying A's."""
    user = await _user(db_session_factory, "reply-g")
    chat = SqlAlchemyChatRepository(db_session_factory)
    first = await chat.add(
        user.id,
        await _thread(db_session_factory, user.id),
        ChatRole.USER,
        "hi!",
        FROZEN_NOW - timedelta(seconds=10),
    )
    second = await chat.add(
        user.id,
        await _thread(db_session_factory, user.id),
        ChatRole.USER,
        "log my ride",
        FROZEN_NOW - timedelta(seconds=5),
    )
    app, gateways = _app(db_session_factory, user)
    app.add_middleware(
        _ModelOverride,
        gateways=gateways,
        model=FunctionModel(_router, stream_function=ScriptedChitchat()),
    )
    client = TestClient(app)

    client.get(f"/messages/{first.id}/reply")
    client.get(f"/messages/{second.id}/reply")

    history = await _history(db_session_factory, user.id)
    assert [(m.role, m.template_id) for m in history] == [
        (ChatRole.USER, None),
        (ChatRole.ASSISTANT, "chitchat"),
        (ChatRole.USER, None),
        (ChatRole.ASSISTANT, "not_yet"),
    ]


# The Jev safety screen (ADR-0020, G3, ticket #121).


async def _calls(
    factory: Callable[[], AsyncSession], user: User
) -> list[tuple[str, LlmCallOutcome]]:
    calls = await SqlAlchemyLlmCallsRepository(factory).list_for_user(user.id)
    return sorted((call.template_id, call.outcome) for call in calls)


async def test_a_greeting_with_pain_the_router_missed_is_screened_and_gets_the_pain_first_reply(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """The reply is pain-first through the chitchat persona, which sees the whole message; the
    screen's added intent is visible only where the router also sent it elsewhere (below)."""
    user = await _user(db_session_factory, "screen-a")
    chitchat, jev = ScriptedChitchat(), FakeJev()

    [body], _ = await _send_and_stream(
        db_session_factory, user, "knee's a bit sore lol, anyway hi", chitchat, jev=jev
    )

    assert jev.states == ["knee's a bit sore lol, anyway hi"]
    [instructions] = chitchat.instructions
    assert "Pain or feeling unwell comes first" in instructions
    assert body.index("Sorry about the knee.") < body.index("Hi!")


async def test_pain_missed_in_a_log_gets_a_pain_first_part_before_not_yet(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "screen-b")

    [body], _ = await _send_and_stream(
        db_session_factory, user, "knee's sore after bouldering, log it", ScriptedChitchat()
    )

    [_, pain, later] = await _history(db_session_factory, user.id)
    assert (pain.template_id, later.template_id) == ("chitchat", "not_yet")
    assert body.index("Sorry about the knee.") < body.index(later.text.split()[0])


async def test_a_greeting_without_pain_gets_plain_chitchat_and_a_screen_row(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "screen-c")
    chitchat = ScriptedChitchat()

    [body], _ = await _send_and_stream(db_session_factory, user, "hi!", chitchat)

    assert "Hi!" in body
    assert "Sorry about the knee." not in body
    calls = await SqlAlchemyLlmCallsRepository(db_session_factory).list_for_user(user.id)
    [screen] = [call for call in calls if call.template_id == "safety_screen"]
    assert (screen.template_version, screen.model, screen.outcome, screen.input_tokens) == (
        1,
        _JEV_SNAPSHOT,
        LlmCallOutcome.SUCCESS,
        120,
    )


async def test_every_message_writes_one_screen_row_under_its_own_decision_id(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "screen-d")
    chat = SqlAlchemyChatRepository(db_session_factory)
    first = await chat.add(
        user.id,
        await _thread(db_session_factory, user.id),
        ChatRole.USER,
        "hi!",
        FROZEN_NOW - timedelta(seconds=10),
    )
    second = await chat.add(
        user.id,
        await _thread(db_session_factory, user.id),
        ChatRole.USER,
        "log my ride",
        FROZEN_NOW - timedelta(seconds=5),
    )
    app, gateways = _app(db_session_factory, user)
    app.add_middleware(
        _ModelOverride,
        gateways=gateways,
        model=FunctionModel(_router, stream_function=ScriptedChitchat()),
    )
    client = TestClient(app)

    client.get(f"/messages/{first.id}/reply")
    client.get(f"/messages/{second.id}/reply")

    assert await _calls(db_session_factory, user) == [
        ("chitchat", LlmCallOutcome.SUCCESS),
        ("router", LlmCallOutcome.SUCCESS),
        ("router", LlmCallOutcome.SUCCESS),
        ("router_fast", LlmCallOutcome.SUCCESS),
        ("router_fast", LlmCallOutcome.SUCCESS),
        ("safety_screen", LlmCallOutcome.SUCCESS),
        ("safety_screen", LlmCallOutcome.SUCCESS),
    ]


async def test_a_screen_timeout_still_streams_the_router_driven_reply(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "screen-e")

    [body], page = await _send_and_stream(
        db_session_factory,
        user,
        "hi!",
        ScriptedChitchat(),
        jev=FakeJev(delay=10),
        decision_timeout=0.1,
    )

    assert "Hi!" in body
    assert "Hi!" in page
    assert ("safety_screen", LlmCallOutcome.TIMEOUT) in await _calls(db_session_factory, user)


# The Jev router fast path (ADR-0020 use 1, ticket #122).

_HI_FAST: FastAnswer = (0.02, {"intent": ("chitchat", 0.97), "sport": ("gym", 0.4)})
_RIDE_FAST: FastAnswer = (0.03, {"intent": ("log_session", 0.96), "sport": ("cycling", 0.95)})


async def test_a_fast_routed_message_writes_a_router_fast_row_and_no_router_row(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "fast-a")
    chitchat = ScriptedChitchat()

    [body], page = await _send_and_stream(
        db_session_factory, user, "hi!", chitchat, jev=FakeJev(fast={"hi!": _HI_FAST})
    )

    assert "Hi!" in body
    assert "Hi!" in page
    assert len(chitchat.instructions) == 1
    assert await _calls(db_session_factory, user) == [
        ("chitchat", LlmCallOutcome.SUCCESS),
        ("router_fast", LlmCallOutcome.SUCCESS),
        ("safety_screen", LlmCallOutcome.SUCCESS),
    ]
    calls = await SqlAlchemyLlmCallsRepository(db_session_factory).list_for_user(user.id)
    [fast] = [call for call in calls if call.template_id == "router_fast"]
    assert (fast.template_version, fast.model, fast.cost) == (1, _JEV_SNAPSHOT, None)


async def test_a_fast_routed_log_takes_its_sport_from_the_athletes_own_sports(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "fast-b")
    await SqlAlchemyOnboardingRepository(db_session_factory).replace_sports(
        user.id, ["climbing", "cycling"]
    )
    jev = FakeJev(fast={"log my ride": _RIDE_FAST})

    await _send_and_stream(db_session_factory, user, "log my ride", ScriptedChitchat(), jev=jev)

    assert jev.fast_asked == [
        {"state": "log my ride", "intent": list(INTENT_KINDS), "sport": ["climbing", "cycling"]}
    ]
    [_, reply] = await _history(db_session_factory, user.id)
    assert (reply.template_id, reply.sports) == ("not_yet", ("cycling",))
    assert "router" not in [name for name, _ in await _calls(db_session_factory, user)]


async def test_several_requests_go_to_the_llm_router(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "fast-c")

    """Jev is sure of the intent and the sport, so only P(several requests), at the
    `Settings` threshold, sends it to the LLM router."""
    user_sports = SqlAlchemyOnboardingRepository(db_session_factory)
    await user_sports.replace_sports(user.id, ["climbing", "cycling"])
    several: FastAnswer = (0.93, {"intent": ("log_session", 0.99), "sport": ("cycling", 0.99)})
    jev = FakeJev(fast={"Ran 5k, also plan my week": several})

    [body], _ = await _send_and_stream(
        db_session_factory, user, "Ran 5k, also plan my week", ScriptedChitchat(), jev=jev
    )

    assert [asked["state"] for asked in jev.fast_asked] == ["Ran 5k, also plan my week"]

    [_, reply] = await _history(db_session_factory, user.id)
    assert reply.template_id == "not_yet"
    assert reply.text.split()[0] in body
    assert await _calls(db_session_factory, user) == [
        ("router", LlmCallOutcome.SUCCESS),
        ("router_fast", LlmCallOutcome.SUCCESS),
        ("safety_screen", LlmCallOutcome.SUCCESS),
    ]


async def test_a_jev_timeout_falls_back_to_the_llm_router_and_the_reply_still_streams(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "fast-d")

    [body], page = await _send_and_stream(
        db_session_factory,
        user,
        "hi!",
        ScriptedChitchat(),
        jev=FakeJev(delay=10, fast={"hi!": _HI_FAST}),
        decision_timeout=0.1,
    )

    assert "Hi!" in body
    assert "Hi!" in page
    assert await _calls(db_session_factory, user) == [
        ("chitchat", LlmCallOutcome.SUCCESS),
        ("router", LlmCallOutcome.SUCCESS),
        ("router_fast", LlmCallOutcome.TIMEOUT),
        ("safety_screen", LlmCallOutcome.TIMEOUT),
    ]


async def test_a_single_sport_athletes_log_gets_their_sport_with_no_sport_question(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "fast-e")
    await SqlAlchemyOnboardingRepository(db_session_factory).replace_sports(user.id, ["gym"])
    jev = FakeJev(fast={"Did an hour": (0.05, {"intent": ("log_session", 0.97)})})

    await _send_and_stream(db_session_factory, user, "Did an hour", ScriptedChitchat(), jev=jev)

    assert jev.fast_asked == [{"state": "Did an hour", "intent": list(INTENT_KINDS)}]
    [_, reply] = await _history(db_session_factory, user.id)
    assert (reply.template_id, reply.sports) == ("not_yet", ("gym",))
    assert "router" not in [name for name, _ in await _calls(db_session_factory, user)]


async def test_the_models_prompt_holds_only_turns_from_the_messages_own_conversation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Through PostgreSQL and the real prompt building: a turn from another conversation never
    reaches the model, while the current conversation's earlier turn does (B13, B24, #146)."""
    user = await _user(db_session_factory, "reply-conversations")
    conversations = SqlAlchemyConversationRepository(db_session_factory)
    chat = SqlAlchemyChatRepository(db_session_factory)
    gym = await conversations.create(user.id, "gym", FROZEN_NOW - timedelta(days=1))
    await chat.add(
        user.id, gym.id, ChatRole.USER, "Squats 3x8 at 100 kg", FROZEN_NOW - timedelta(days=1)
    )
    current = await conversations.create(user.id, "ride", FROZEN_NOW - timedelta(hours=1))
    await chat.add(
        user.id, current.id, ChatRole.USER, "Rode 40 km", FROZEN_NOW - timedelta(hours=1)
    )
    chitchat = ScriptedChitchat()
    router = _RecordingRouter()

    await _send_and_stream(db_session_factory, user, "hi!", chitchat, router=router)

    # The safety screen's Jev call declines here, so the LLM router runs too.
    for prompt in (*router.prompts, *chitchat.prompts):
        assert "Rode 40 km" in str(prompt)
        assert "Squats" not in str(prompt)
    assert (len(router.prompts), len(chitchat.prompts)) == (1, 1)
    assert [m.text for m in await _history(db_session_factory, user.id)][:2] == [
        "Rode 40 km",
        "hi!",
    ]


async def test_another_users_conversation_is_404_to_open_and_to_send_to(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Through PostgreSQL: user B can't open user A's conversation or add to it, and an
    unknown conversation is as missing as theirs (ADR-0021, #146 AC 4)."""
    owner = await _user(db_session_factory, "reply-conversation-owner")
    stranger = await _user(db_session_factory, "reply-conversation-stranger")
    theirs = await SqlAlchemyConversationRepository(db_session_factory).create(
        owner.id, "private", FROZEN_NOW
    )
    await SqlAlchemyChatRepository(db_session_factory).add(
        owner.id, theirs.id, ChatRole.USER, "for the owner only", FROZEN_NOW
    )
    client = TestClient(_app(db_session_factory, stranger)[0])

    for headers in ({}, _HTMX):
        opened = client.get(f"/c/{theirs.id}", headers=headers)
        assert opened.status_code == 404
        assert "for the owner only" not in opened.text
    assert client.get(f"/c/{uuid4()}").status_code == 404
    sent = client.post(
        "/messages",
        data={"message": "sneaking in", "conversation_id": str(theirs.id)},
        headers=_HTMX,
    )

    assert sent.status_code == 404
    assert [m.text for m in await _history(db_session_factory, owner.id)] == ["for the owner only"]
    assert (
        await SqlAlchemyConversationRepository(db_session_factory).list_recent(stranger.id, 10)
        == []
    )
