"""The streamed reply end to end on PostgreSQL (ticket #82): the real gateway, repositories and
prompt templates, with OpenRouter replaced by a scripted `FunctionModel` (ADR-0007). Every
model call leaves its `llm_calls` row (ADR-0018), and a reply survives a reload."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.reply import ReplyServices
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.llm.conversation import LlmConversation, build_prompt_registry
from ai_trainer.llm.gateway import OpenRouterGateway
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
}


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


class ScriptedChitchat:
    """Streams a reply; the first `hangs` calls never answer, so the gateway times out."""

    def __init__(self, hangs: int = 0) -> None:
        self.hangs = hangs
        self.instructions: list[str] = []

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        self.instructions.append(str(request.instructions))
        if self.hangs:
            self.hangs -= 1
            await asyncio.sleep(10)
        for chunk in ("Sorry about the knee. ", "Hi!") if "knee" in _prompt(messages) else ("Hi!",):
            yield chunk


class _ModelOverride:
    """Runs every request, streamed body included, under `agent.override`, which is a
    ContextVar: the test client serves the app on its own thread, outside the test's context."""

    def __init__(self, app: ASGIApp, *, gateway: OpenRouterGateway, model: Model) -> None:
        self.app, self.gateway, self.model = app, gateway, model

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with self.gateway.agent.override(model=self.model):
            await self.app(scope, receive, send)


async def _user(factory: Callable[[], AsyncSession], sub: str) -> User:
    return await SqlAlchemyUsersRepository(factory).create(
        NewUser(sub=sub, email=f"{sub}@x.io", name="Ada", locale="en", status=UserStatus.ACTIVE)
    )


def _app(
    factory: Callable[[], AsyncSession], user: User, *, timeout: float = 30.0
) -> tuple[FastAPI, OpenRouterGateway]:
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        llm_call_timeout_seconds=timeout,
    )
    gateway = OpenRouterGateway(settings, SqlAlchemyLlmCallsRepository(factory))
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
        profile=SqlAlchemyOnboardingRepository(factory),
        conversation=conversation,
        clock=FixedClock(),
        history_turns=settings.chat_history_turns,
    )
    app = FastAPI()
    app.include_router(build_chat_router(build_templates(), chat=chat, replies=replies))
    sign_in_as(app, user)
    return app, gateway


async def _send_and_stream(
    factory: Callable[[], AsyncSession],
    user: User,
    text: str,
    chitchat: ScriptedChitchat,
    *,
    timeout: float = 30.0,
    streams: int = 1,
) -> tuple[list[str], str]:
    """Sends `text`, reads its reply stream `streams` times (a retry after the first) and
    reloads the page."""
    app, gateway = _app(factory, user, timeout=timeout)
    model = FunctionModel(_router, stream_function=chitchat)
    app.add_middleware(_ModelOverride, gateway=gateway, model=model)
    client = TestClient(app)
    client.post("/messages", data={"message": text}, headers=_HTMX)
    [asked] = await SqlAlchemyChatRepository(factory).list_recent(user.id, 1)
    bodies = [client.get(f"/messages/{asked.id}/reply").text for _ in range(streams)]
    return bodies, client.get("/").text


async def test_hi_streams_a_reply_that_survives_a_reload_with_a_row_per_model_call(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "reply-a")

    [body], page = await _send_and_stream(db_session_factory, user, "hi!", ScriptedChitchat())

    assert "Hi!" in body
    assert "Hi!" in page
    messages = await SqlAlchemyChatRepository(db_session_factory).list_recent(user.id, 50)
    assert [(m.role, m.template_id) for m in messages] == [
        (ChatRole.USER, None),
        (ChatRole.ASSISTANT, "chitchat"),
    ]
    calls = await SqlAlchemyLlmCallsRepository(db_session_factory).list_for_user(user.id)
    assert sorted((call.template_id, call.outcome) for call in calls) == [
        ("chitchat", LlmCallOutcome.SUCCESS),
        ("router", LlmCallOutcome.SUCCESS),
    ]


async def test_log_my_ride_gets_the_not_yet_reply_and_only_the_router_row(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory, "reply-b")
    chitchat = ScriptedChitchat()

    await _send_and_stream(db_session_factory, user, "log my ride", chitchat)

    assert chitchat.instructions == []
    [_, reply] = await SqlAlchemyChatRepository(db_session_factory).list_recent(user.id, 50)
    assert (reply.template_id, reply.template_version, reply.sports) == ("not_yet", 1, ("cycling",))
    calls = await SqlAlchemyLlmCallsRepository(db_session_factory).list_for_user(user.id)
    assert [call.template_id for call in calls] == ["router"]


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
    ]


async def test_another_users_message_is_404_for_its_reply_and_retry_with_no_model_call(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    owner = await _user(db_session_factory, "reply-e")
    stranger = await _user(db_session_factory, "reply-f")
    theirs = await SqlAlchemyChatRepository(db_session_factory).add(
        owner.id, ChatRole.USER, "hi!", FROZEN_NOW
    )
    app, gateway = _app(db_session_factory, stranger)
    app.add_middleware(
        _ModelOverride,
        gateway=gateway,
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
    first = await chat.add(user.id, ChatRole.USER, "hi!", FROZEN_NOW - timedelta(seconds=10))
    second = await chat.add(
        user.id, ChatRole.USER, "log my ride", FROZEN_NOW - timedelta(seconds=5)
    )
    app, gateway = _app(db_session_factory, user)
    app.add_middleware(
        _ModelOverride,
        gateway=gateway,
        model=FunctionModel(_router, stream_function=ScriptedChitchat()),
    )
    client = TestClient(app)

    client.get(f"/messages/{first.id}/reply")
    client.get(f"/messages/{second.id}/reply")

    history = await chat.list_recent(user.id, 50)
    assert [(m.role, m.template_id) for m in history] == [
        (ChatRole.USER, None),
        (ChatRole.ASSISTANT, "chitchat"),
        (ChatRole.USER, None),
        (ChatRole.ASSISTANT, "not_yet"),
    ]
