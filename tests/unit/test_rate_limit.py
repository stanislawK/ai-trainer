"""In-process request and model-call limits (ADR-0029)."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, PostgresDsn, SecretStr
from starlette.types import Message, Receive, Scope, Send

from ai_trainer.application.budgeted_gateways import BudgetedDecisionGateway, BudgetedLlmGateway
from ai_trainer.application.model_budget import ModelCallBudget
from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.application.ports.llm_gateway import LlmGatewayResult, LlmStreamEvent, StreamEnd
from ai_trainer.application.sliding_window import SlidingWindow
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.llm_calls import LlmCallOutcome, ProviderPreferences, ReasoningEffort
from ai_trainer.main import create_app
from ai_trainer.settings import Settings
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.rate_limit import RateLimitMiddleware, _client_address
from ai_trainer.web.templating import build_templates
from tests.chat_support import FROZEN_NOW, ScriptedConversation, include_chat, sign_in_as

_USER = uuid4()
_NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
_HOUR = Settings.model_fields["llm_calls_per_user_per_hour"].default
_AUTH = Settings.model_fields["auth_requests_per_minute"].default
_SESSION = Settings.model_fields["session_requests_per_minute"].default
_WAIT = "You've sent a lot in the last hour."


class FakeClock:
    def __init__(self, now: datetime = _NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, by: timedelta) -> None:
        self._now += by


class RecordingGateway:
    """Stands in for both gateways. `release` holds a call that has already counted."""

    def __init__(self) -> None:
        self.runs = 0
        self.streams = 0
        self.decisions = 0
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def run[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        template_id: str,
        template_version: int,
        model_id: str,
        output_type: type[OutputT] | tuple[type[OutputT], ...],
        instructions: str,
        prompt: str,
        temperature: float | None = None,
        output_retries: int | None = None,
        provider: ProviderPreferences | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> LlmGatewayResult[OutputT]:
        self.runs += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return LlmGatewayResult(output=None, friendly_error=None, outcome=LlmCallOutcome.SUCCESS)

    async def stream(
        self,
        *,
        user_id: UUID,
        template_id: str,
        template_version: int,
        model_id: str,
        instructions: str,
        prompt: str,
    ) -> AsyncGenerator[LlmStreamEvent]:
        self.streams += 1
        yield StreamEnd(
            result=LlmGatewayResult(
                output="ok", friendly_error=None, outcome=LlmCallOutcome.SUCCESS
            )
        )

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        self.decisions += 1
        return DecisionResult(output=None, outcome=LlmCallOutcome.SUCCESS)


def _budget(clock: FakeClock, *, per_hour: int, in_flight: int) -> ModelCallBudget:
    return ModelCallBudget(clock, per_hour=per_hour, in_flight=in_flight)


def _text(gateway: RecordingGateway, budget: ModelCallBudget) -> BudgetedLlmGateway:
    return BudgetedLlmGateway(gateway, budget)


async def _run(gateway: BudgetedLlmGateway) -> LlmGatewayResult[BaseModel]:
    return await gateway.run(
        user_id=_USER,
        template_id="router",
        template_version=1,
        model_id="test/model",
        output_type=BaseModel,
        instructions="",
        prompt="hello",
    )


def _final(body: str) -> str:
    events = [block for block in body.replace("\r\n", "\n").split("\n\n") if block.strip()]
    return "\n".join(
        line.removeprefix("data: ") for line in events[-1].split("\n") if line.startswith("data:")
    )


def test_the_defaults_leave_room_for_onboarding_and_a_first_hour_of_logs() -> None:
    assert _HOUR == 300
    assert _AUTH == 10
    assert _SESSION == 120
    assert Settings.model_fields["llm_calls_in_flight_per_user"].default == 8


async def test_the_301st_model_call_in_an_hour_is_not_sent() -> None:
    gateway = RecordingGateway()
    wrapped = _text(gateway, _budget(FakeClock(), per_hour=_HOUR, in_flight=_HOUR))

    for _ in range(_HOUR):
        allowed = await _run(wrapped)
        assert allowed.outcome is LlmCallOutcome.SUCCESS
    refused = await _run(wrapped)

    assert refused.outcome is LlmCallOutcome.RATE_LIMITED
    assert refused.friendly_error is None
    assert gateway.runs == _HOUR


async def test_a_cancelled_call_frees_its_in_flight_slot_and_still_counts_for_the_hour() -> None:
    gateway = RecordingGateway()
    gateway.release = asyncio.Event()
    wrapped = _text(gateway, _budget(FakeClock(), per_hour=2, in_flight=1))
    started = asyncio.create_task(_run(wrapped))
    await gateway.started.wait()

    started.cancel()
    with pytest.raises(asyncio.CancelledError):
        await started

    gateway.release.set()
    gateway.started.clear()
    gateway.release = None
    second = await _run(wrapped)
    refused = await _run(wrapped)

    assert second.outcome is LlmCallOutcome.SUCCESS
    assert refused.outcome is LlmCallOutcome.RATE_LIMITED
    assert gateway.runs == 2


async def test_an_in_flight_refusal_does_not_count_and_one_release_frees_one_slot() -> None:
    gateway = RecordingGateway()
    gateway.release = asyncio.Event()
    wrapped = _text(gateway, _budget(FakeClock(), per_hour=10, in_flight=2))
    first = asyncio.create_task(_run(wrapped))
    await gateway.started.wait()
    gateway.started.clear()
    second = asyncio.create_task(_run(wrapped))
    await gateway.started.wait()

    refused = await _run(wrapped)
    assert refused.outcome is LlmCallOutcome.RATE_LIMITED
    assert gateway.runs == 2

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gateway.started.clear()
    third = asyncio.create_task(_run(wrapped))
    await gateway.started.wait()
    still_full = await _run(wrapped)

    gateway.release.set()
    await second
    await third
    assert still_full.outcome is LlmCallOutcome.RATE_LIMITED
    assert gateway.runs == 3


async def test_a_call_is_allowed_again_once_its_hour_has_passed() -> None:
    clock = FakeClock()
    gateway = RecordingGateway()
    wrapped = _text(gateway, _budget(clock, per_hour=1, in_flight=1))

    assert (await _run(wrapped)).outcome is LlmCallOutcome.SUCCESS
    assert (await _run(wrapped)).outcome is LlmCallOutcome.RATE_LIMITED
    clock.advance(timedelta(hours=1))
    assert (await _run(wrapped)).outcome is LlmCallOutcome.SUCCESS
    assert gateway.runs == 2


async def test_a_refused_stream_and_a_decision_share_the_text_budget() -> None:
    clock = FakeClock()
    gateway = RecordingGateway()
    budget = _budget(clock, per_hour=1, in_flight=2)
    text = _text(gateway, budget)
    decisions = BudgetedDecisionGateway(gateway, budget)

    assert (await _run(text)).outcome is LlmCallOutcome.SUCCESS
    refused = await decisions.decide(
        user_id=_USER,
        decision_id="safety_screen",
        decision_version=1,
        output_type=BaseModel,
        state="hello",
    )
    events = [
        event
        async for event in text.stream(
            user_id=_USER,
            template_id="chitchat",
            template_version=1,
            model_id="test/model",
            instructions="",
            prompt="hello",
        )
    ]

    assert refused.outcome is LlmCallOutcome.RATE_LIMITED
    assert gateway.decisions == 0
    assert gateway.streams == 0
    assert len(events) == 1
    assert isinstance(events[0], StreamEnd)
    assert events[0].result.outcome is LlmCallOutcome.RATE_LIMITED


async def test_an_allowed_stream_and_decision_reach_the_gateway() -> None:
    gateway = RecordingGateway()
    budget = _budget(FakeClock(), per_hour=2, in_flight=2)
    text = _text(gateway, budget)
    decisions = BudgetedDecisionGateway(gateway, budget)

    events = [
        event
        async for event in text.stream(
            user_id=_USER,
            template_id="chitchat",
            template_version=1,
            model_id="test/model",
            instructions="",
            prompt="hello",
        )
    ]
    decided = await decisions.decide(
        user_id=_USER,
        decision_id="router_fast",
        decision_version=1,
        output_type=BaseModel,
        state="hello",
    )

    assert gateway.streams == 1
    assert gateway.decisions == 1
    assert isinstance(events[0], StreamEnd)
    assert events[0].result.outcome is LlmCallOutcome.SUCCESS
    assert decided.outcome is LlmCallOutcome.SUCCESS


def test_idle_keys_are_swept_so_random_cookies_do_not_grow_memory() -> None:
    clock = FakeClock()
    window = SlidingWindow(clock, window=timedelta(minutes=1))
    for n in range(100):
        assert window.allow(f"session:{n}", limit=1) is None
    clock.advance(timedelta(seconds=30))
    assert window.allow("session:recent", limit=1) is None

    clock.advance(timedelta(seconds=31))
    assert window.allow("session:new", limit=1) is None

    assert len(window) == 2
    assert window.allow("session:recent", limit=1) is not None


def test_a_kept_key_drops_its_old_events_and_the_wait_is_rounded_up() -> None:
    clock = FakeClock()
    window = SlidingWindow(clock, window=timedelta(minutes=1))
    assert window.allow("auth:a", limit=2) is None
    clock.advance(timedelta(seconds=40))
    assert window.allow("auth:a", limit=2) is None

    clock.advance(timedelta(seconds=0.5))
    assert window.allow("auth:a", limit=2) == 20
    clock.advance(timedelta(seconds=20))
    assert window.allow("auth:a", limit=2) is None
    assert window.allow("auth:a", limit=2) == 40


def _http_app(clock: FakeClock, *, auth: int = _AUTH, session: int = _SESSION) -> FastAPI:
    app = FastAPI()

    @app.api_route("/{full_path:path}", methods=["GET", "POST"])
    async def anything(full_path: str) -> dict[str, str]:
        return {"path": full_path}

    app.add_middleware(
        RateLimitMiddleware,
        clock=clock,
        templates=build_templates(),
        auth_per_minute=auth,
        session_per_minute=session,
    )
    return app


def _hit(client: TestClient, path: str, *, headers: dict[str, str] | None = None) -> int:
    return client.get(path, headers=headers).status_code


def test_the_11th_sign_in_from_one_address_is_refused_and_another_address_is_not() -> None:
    app = _http_app(FakeClock())
    first = TestClient(app)
    second = TestClient(app)
    headers = {"CF-Connecting-IP": "203.0.113.5"}
    other = {"CF-Connecting-IP": "203.0.113.6"}

    assert [_hit(first, "/auth/login", headers=headers) for _ in range(_AUTH)] == [200] * _AUTH
    limited = first.get("/auth/callback", headers=headers)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert "Slow down a moment" in limited.text
    assert "<html" in limited.text
    partial = first.get("/sign-in", headers={**headers, "HX-Request": "true"})
    assert partial.status_code == 429
    assert "Slow down a moment" in partial.text
    assert "<html" not in partial.text
    assert _hit(second, "/auth/login", headers=other) == 200


def test_a_missing_or_unusable_address_header_uses_the_peer() -> None:
    app = _http_app(FakeClock(), auth=1)
    one = TestClient(app, client=("203.0.113.8", 1234))
    two = TestClient(app, client=("203.0.113.9", 1234))
    bogus = {"CF-Connecting-IP": "not an address\r\n203.0.113.1"}

    assert one.get("/auth/login", headers=bogus).status_code == 200
    assert one.get("/auth/login").status_code == 429
    assert two.get("/auth/login", headers=bogus).status_code == 200
    assert _client_address({"type": "http", "headers": []}) == "unknown"


def test_health_and_static_are_not_limited_and_a_session_is() -> None:
    app = _http_app(FakeClock(), auth=1, session=2)
    client = TestClient(app)
    for path in ("/health", "/health/", "/static/app.css"):
        assert _hit(client, path) == 200
        assert _hit(client, path) == 200
    assert [client.get("/messages").status_code for _ in range(5)] == [200] * 5

    client.cookies.set(SESSION_COOKIE_NAME, "session-a")
    assert client.get("/messages").status_code == 200
    assert client.get("/messages").status_code == 200
    limited = client.post("/messages")
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers

    other = TestClient(app)
    other.cookies.set(SESSION_COOKIE_NAME, "session-b")
    assert other.get("/messages").status_code == 200


def test_the_app_refuses_the_sign_in_page_past_its_configured_cap() -> None:
    app = create_app(
        Settings(
            _env_file=None,
            database_url=PostgresDsn(
                "postgresql+psycopg://ai_trainer:secret@localhost:5432/ai_trainer"
            ),
            openrouter_api_key=SecretStr("sk-or-v1-test"),
            eval_judge_model="test/judge-model",
            auth_requests_per_minute=2,
        )
    )
    client = TestClient(app)

    assert client.get("/sign-in").status_code == 200
    assert client.get("/sign-in").status_code == 200
    limited = client.get("/sign-in")
    health = client.get("/health")

    assert limited.status_code == 429
    assert "Slow down a moment" in limited.text
    assert health.status_code != 429


async def test_a_rate_limited_reply_tells_the_athlete_to_wait() -> None:
    app = FastAPI()
    chat = include_chat(
        app,
        build_templates(),
        conversation=ScriptedConversation(route_outcomes=[LlmCallOutcome.RATE_LIMITED]),
    )
    user = sign_in_as(app)
    client = TestClient(app)
    asked = await chat.add(
        user.id, await chat.conversation_of(user.id), ChatRole.USER, "hi", FROZEN_NOW
    )

    final = _final(client.get(f"/messages/{asked.id}/reply").text)

    assert _WAIT in final
    assert "The reply didn't come through." not in final
    assert f'hx-get="/messages/{asked.id}/retry"' in final


async def test_a_non_http_connection_is_not_limited() -> None:
    called: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        called.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    middleware = RateLimitMiddleware(
        inner,
        clock=FakeClock(),
        templates=build_templates(),
        auth_per_minute=1,
        session_per_minute=1,
    )
    await middleware({"type": "lifespan"}, receive, send)

    assert called == ["lifespan"]
