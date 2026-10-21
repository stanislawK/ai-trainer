"""`make latency-scenario` (ADR-0026 step 1, ticket #185): it drives the chat over HTTP as a
browser does, times each turn from send to card, and adds up the turn's `llm_calls` rows. Here
it drives the real chat router in-process, with a scripted conversation that leaves an
`llm_calls` row for every model call it stands in for."""

import io
import json
import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import FastAPI

from ai_trainer.application.ports.conversation import Extraction, ReplyContext, Routing
from ai_trainer.application.ports.llm_gateway import LlmStreamEvent
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCall, LlmCallOutcome
from ai_trainer.web.chat import build_chat_router
from ai_trainer.web.templating import build_templates
from scripts.dev_session import DEV_SESSION_SUB
from scripts.latency_scenario import (
    DEV_LATENCY_SUB,
    SCENARIO,
    CallRow,
    StackDownError,
    Turn,
    TurnResult,
    check_stack,
    main,
    parse_args,
    play,
    render,
    seed_latency_athlete,
    summarize,
    write_json,
)
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryChatRepository,
    ScriptedConversation,
    reply_services,
    sign_in_as,
    sports_with_rowing,
)
from tests.unit.test_dev_session import (
    FakeClock,
    FakeOnboardingRepository,
    FakeSessionsRepository,
    FakeUsersRepository,
)

_BASE = "http://testserver"
_LOG = [Intent(kind="log_session", sport="climbing")]
_CHITCHAT = [Intent(kind="chitchat")]


class FakeLlmCalls:
    """The `llm_calls` rows of one user, appended as the scripted calls happen."""

    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id
        self.rows: list[LlmCall] = []
        self.asked_for: list[UUID] = []

    def add(self, template_id: str, *, latency_ms: int, cost: str | None, outcome: str) -> None:
        self.rows.append(
            LlmCall(
                id=uuid4(),
                user_id=self.user_id,
                template_id=template_id,
                template_version=1,
                model="z-ai/glm-5.3-flash",
                input_tokens=2000,
                output_tokens=300,
                cost=None if cost is None else Decimal(cost),
                latency_ms=latency_ms,
                outcome=LlmCallOutcome(outcome),
                created_at=FROZEN_NOW + timedelta(seconds=len(self.rows)),
            )
        )

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        self.asked_for.append(user_id)
        return [row for row in self.rows if row.user_id == user_id]


@dataclass
class AccountedConversation(ScriptedConversation):
    """A scripted conversation whose every call leaves its `llm_calls` row, as the gateway
    does (ADR-0018): a Jev row with no cost, then the router's, the extraction's or chitchat's."""

    calls: FakeLlmCalls | None = None
    costs: Iterator[str] = field(default_factory=lambda: iter(()))

    def _row(self, template_id: str, outcome: LlmCallOutcome) -> None:
        assert self.calls is not None
        self.calls.add(template_id, latency_ms=900, cost=next(self.costs), outcome=outcome)

    async def route(self, context: ReplyContext) -> Routing:
        assert self.calls is not None
        self.calls.add("router_fast", latency_ms=340, cost=None, outcome="success")
        routing = await super().route(context)
        self._row("router", routing.outcome)
        return routing

    async def extract(
        self,
        context: ReplyContext,
        sport_id: str,
        *,
        recent: Sequence[str] = (),
        answers: Sequence[str] = (),
        may_ask: bool = True,
    ) -> Extraction:
        extraction = await super().extract(
            context, sport_id, recent=recent, answers=answers, may_ask=may_ask
        )
        self._row(f"{sport_id}.extract", extraction.outcome)
        return extraction

    async def stream_chitchat(self, context: ReplyContext) -> AsyncGenerator[LlmStreamEvent]:
        async for event in super().stream_chitchat(context):
            yield event
        self._row("chitchat", LlmCallOutcome.SUCCESS)


class TickingClock:
    """The app's clock: a second later on every reading, so each message and reply part of a
    conversation has its own time, as on the running app."""

    def __init__(self) -> None:
        self.current = FROZEN_NOW

    def now(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


class SteppingTimer:
    """A clock for `play`: each reading is `step` seconds after the last."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _app(conversation: AccountedConversation) -> tuple[FastAPI, FakeLlmCalls]:
    app = FastAPI()
    chat = InMemoryChatRepository()
    services = replace(reply_services(chat, conversation=conversation), clock=TickingClock())
    app.include_router(
        build_chat_router(
            build_templates(), chat=chat, replies=services, sports=sports_with_rowing()
        )
    )
    user = sign_in_as(app)
    calls = FakeLlmCalls(user.id)
    conversation.calls = calls
    return app, calls


def _client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=_BASE)


async def _play(
    conversation: AccountedConversation, turns: tuple[Turn, ...], *, step: float = 0.5
) -> list[TurnResult]:
    app, calls = _app(conversation)
    async with _client(app) as client:
        return await play(
            client, calls, calls.user_id, csrf_token="token", turns=turns, timer=SteppingTimer(step)
        )


_TWO_LOGS_AND_CHITCHAT = (
    Turn("sent three 6b problems", log=True, confirm=True),
    Turn("tried a 7a project three times", log=True, confirm=False),
    Turn("Thanks, that's all for today!", log=False, confirm=False),
)


def _results(*ms: int, log: bool = True) -> list[TurnResult]:
    return [
        TurnResult(number=n, text=f"log {n}", log=log, ms=value, outcome="ok", calls=())
        for n, value in enumerate(ms, start=1)
    ]


def test_the_scenario_is_adr_0025s_five_turns_then_eight_follow_up_logs() -> None:
    first, second, third, fourth, chitchat, *follow_ups = SCENARIO

    assert first.text == "Bouldering at the gym tonight, sent three 6b problems and flashed a 6a"
    assert [first.confirm, second.confirm, third.confirm, fourth.confirm] == [True] * 3 + [False]
    assert chitchat == Turn("Thanks, that's all for today!", log=False, confirm=False)
    assert len(follow_ups) == 8
    assert all(turn.log and turn.confirm for turn in follow_ups)


async def test_every_turn_is_timed_from_send_to_its_card_in_one_conversation() -> None:
    conversation = AccountedConversation(
        intents=[_LOG, _LOG, _CHITCHAT], costs=iter(["0.0001"] * 6)
    )

    results = await _play(conversation, _TWO_LOGS_AND_CHITCHAT)

    assert [result.outcome for result in results] == ["ok", "ok", "ok"]
    # The timer steps 0.5 s per reading and a turn reads it at send and at the card.
    assert [result.ms for result in results] == [500, 500, 500]
    assert all(result.draft_id is not None for result in results[:2])
    # Every turn after the first went to the conversation the first one started.
    seen = [message.text for message in conversation.routed[-1].history]
    assert seen[0] == "sent three 6b problems"
    assert "tried a 7a project three times" in seen


async def test_a_confirmed_log_is_saved_and_an_unconfirmed_one_stays_pending() -> None:
    conversation = AccountedConversation(intents=[_LOG], costs=iter(["0.0001"] * 4))
    app, calls = _app(conversation)

    async with _client(app) as client:
        results = await play(
            client,
            calls,
            calls.user_id,
            csrf_token="token",
            turns=_TWO_LOGS_AND_CHITCHAT[:2],
            timer=SteppingTimer(0.1),
        )
        confirmed = await client.get(f"/drafts/{results[0].draft_id}")
        pending = await client.get(f"/drafts/{results[1].draft_id}")

    assert "Confirm" not in confirmed.text
    assert "Confirm" in pending.text


async def test_turn_usd_is_the_sum_of_its_llm_calls_rows() -> None:
    conversation = AccountedConversation(
        intents=[_LOG, _CHITCHAT], costs=iter(["0.000120", "0.000305", "0.000050", "0.000077"])
    )

    log, chitchat = await _play(conversation, _TWO_LOGS_AND_CHITCHAT[::2])

    assert [call.template_id for call in log.calls] == ["router_fast", "router", "climbing.extract"]
    # Jev's row has no cost and adds nothing (ADR-0018).
    assert log.usd == Decimal("0.000425")
    assert [call.template_id for call in chitchat.calls] == ["router_fast", "router", "chitchat"]
    assert chitchat.usd == Decimal("0.000127")


async def test_a_timed_out_reply_is_counted_and_named() -> None:
    conversation = AccountedConversation(
        intents=[_LOG],
        extract_outcomes=[LlmCallOutcome.SUCCESS, LlmCallOutcome.TIMEOUT],
        costs=iter(["0.0001"] * 4),
    )

    results = await _play(conversation, _TWO_LOGS_AND_CHITCHAT[:2])
    summary = summarize(results)

    assert [result.outcome for result in results] == ["ok", "timeout"]
    assert results[1].draft_id is None
    assert results[1].calls[-1].outcome == "timeout"
    assert summary.timeouts == [2]
    assert "#2 timeout: tried a 7a project three times" in render(summary)


async def test_a_failed_reply_is_counted_and_named() -> None:
    conversation = AccountedConversation(
        intents=[_LOG], route_outcomes=[LlmCallOutcome.ERROR], costs=iter(["0.0001"])
    )

    [result] = await _play(conversation, _TWO_LOGS_AND_CHITCHAT[:1])
    summary = summarize([result])

    assert result.outcome == "error"
    assert summary.failures == [1]
    assert "#1 error: sent three 6b problems" in render(summary)


async def test_a_log_whose_reply_shows_no_card_is_a_failure() -> None:
    conversation = AccountedConversation(intents=[_CHITCHAT], costs=iter(["0.0001"] * 2))

    [result] = await _play(conversation, _TWO_LOGS_AND_CHITCHAT[:1])

    assert result.outcome == "no_card"
    assert summarize([result]).failures == [1]


async def test_a_refused_message_is_a_failure_and_the_run_goes_on() -> None:
    conversation = AccountedConversation(intents=[_LOG], costs=iter(["0.0001"] * 2))
    turns = (Turn("", log=True, confirm=True), _TWO_LOGS_AND_CHITCHAT[0])

    empty, log = await _play(conversation, turns)

    assert empty.outcome == "http_422"
    assert empty.calls == ()
    assert log.outcome == "ok"


def test_p50_and_p95_are_over_log_turns_only_by_nearest_rank() -> None:
    results = [*_results(1000, 2000, 3000, 4000, 20000), *_results(90000, log=False)]

    summary = summarize(results)

    assert (summary.log_turns, summary.p50_ms, summary.p95_ms) == (5, 3000, 20000)
    assert "Log turns: 5 · p50 3.0 s · p95 20.0 s · timeouts 0 · failures 0" in render(summary)


def test_the_summary_with_no_log_turn_has_no_percentiles() -> None:
    summary = summarize(_results(1000, log=False))

    assert (summary.p50_ms, summary.p95_ms) == (None, None)
    assert "Log turns: 0 · p50 n/a · p95 n/a" in render(summary)


def test_the_table_lists_each_turn_with_its_calls_and_usd() -> None:
    call = CallRow(
        template_id="climbing.extract",
        model="z-ai/glm-5.3-flash",
        latency_ms=12603,
        input_tokens=1381,
        output_tokens=548,
        cost=Decimal("0.000310"),
        outcome="success",
    )
    result = TurnResult(
        number=1, text="sent three 6b", log=True, ms=34210, outcome="ok", calls=(call,)
    )

    table = render(summarize([result]))

    assert "34.2" in table
    assert "0.000310" in table
    assert "climbing.extract 12603 ms 1381→548 tok" in table


def test_the_summary_is_written_as_json_under_the_debug_dir(tmp_path: Path) -> None:
    timed_out = TurnResult(
        number=2, text="7a project", log=True, ms=30500, outcome="timeout", calls=()
    )
    summary = summarize([*_results(1000), timed_out])

    path = write_json(summary, tmp_path / ".llm-debug", now=FROZEN_NOW)

    assert path == tmp_path / ".llm-debug" / "latency-scenario-20261002T093000Z.json"
    written = json.loads(path.read_text())
    assert written["log_turns"] == 2
    assert written["p50_ms"] == 1000
    assert written["timeouts"] == [2]
    assert written["turns"][1]["outcome"] == "timeout"


async def test_check_stack_passes_when_health_answers() -> None:
    def healthy(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"database": "ok"})

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(healthy), base_url=_BASE
    ) as client:
        await check_stack(client)


async def test_check_stack_fails_on_an_unhealthy_answer() -> None:
    def unhealthy(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503)

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(unhealthy), base_url=_BASE
    ) as client:
        with pytest.raises(StackDownError, match="503"):
            await check_stack(client)


async def test_check_stack_fails_when_the_database_is_down() -> None:
    def no_database(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"database": "unavailable"})

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(no_database), base_url=_BASE
    ) as client:
        with pytest.raises(StackDownError, match="database is down"):
            await check_stack(client)


async def test_with_the_stack_down_it_fails_fast_and_sends_nothing() -> None:
    requested: list[str] = []

    def refuse(request: httpx2.Request) -> httpx2.Response:
        requested.append(f"{request.method} {request.url.path}")
        raise httpx2.ConnectError("Connection refused", request=request)

    errors = io.StringIO()

    code = await main(
        base_url=_BASE, output=io.StringIO(), errors=errors, transport=httpx2.MockTransport(refuse)
    )

    assert code == 1
    assert requested == ["GET /health"]
    assert "make start" in errors.getvalue()
    assert f"{_BASE}/health" in errors.getvalue()


async def test_the_latency_athlete_is_an_onboarded_climbing_only_user_of_its_own() -> None:
    users = FakeUsersRepository()
    onboarding = FakeOnboardingRepository()

    session = await seed_latency_athlete(
        users,
        FakeSessionsRepository(),
        FakeClock(),
        onboarding=onboarding,
        session_ttl=timedelta(days=1),
    )

    user = users.users[DEV_LATENCY_SUB]
    assert DEV_LATENCY_SUB != DEV_SESSION_SUB
    assert session.user_id == user.id
    assert onboarding.sports[user.id] == ["climbing"]
    assert onboarding.onboarded_at[user.id] is not None


_MESSAGE_ID = "7f1c3a52-9a7e-4b39-9d5e-0c0a8e6b2f11"
_DRAFT = "0d5e2c8a-4b1f-4e7a-9c3d-5a6b7c8d9e0f"


def _stub_app(
    reply: Callable[[httpx2.Request], httpx2.Response], *, confirm_status: int = 200
) -> httpx2.AsyncClient:
    """An app that accepts every message and answers its reply stream with `reply`."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/messages":
            sent = f'<div hx-sse:connect="/messages/{_MESSAGE_ID}/reply"></div>'
            return httpx2.Response(200, text=sent)
        if request.url.path.endswith("/confirm"):
            return httpx2.Response(confirm_status)
        return reply(request)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle), base_url=_BASE)


async def _play_stub(client: httpx2.AsyncClient) -> TurnResult:
    async with client:
        [result] = await play(
            client,
            FakeLlmCalls(uuid4()),
            uuid4(),
            csrf_token="token",
            turns=_TWO_LOGS_AND_CHITCHAT[:1],
            timer=SteppingTimer(1),
        )
    return result


async def test_a_last_event_without_a_closing_blank_line_still_counts() -> None:
    final = f'data: <template hx-target="#reply-{_MESSAGE_ID}"><p data-draft-id="{_DRAFT}">'

    result = await _play_stub(_stub_app(lambda _: httpx2.Response(200, text=final)))

    assert (result.outcome, result.draft_id) == ("ok", UUID(_DRAFT))


async def test_a_reply_stream_that_ends_before_its_final_event_is_a_failure() -> None:
    chunk = f'data: <template hx-target="#reply-{_MESSAGE_ID}-text">Hey</template>\n\n'

    result = await _play_stub(_stub_app(lambda _: httpx2.Response(200, text=chunk)))

    assert result.outcome == "stream_ended"
    assert summarize([result]).failures == [1]


async def test_a_refused_reply_stream_is_a_failure() -> None:
    result = await _play_stub(_stub_app(lambda _: httpx2.Response(404)))

    assert result.outcome == "http_404"


async def test_a_reply_the_client_gives_up_waiting_for_is_a_timeout() -> None:
    def stuck(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("no event", request=request)

    result = await _play_stub(_stub_app(stuck))

    assert result.outcome == "timeout"
    assert summarize([result]).timeouts == [1]


async def test_a_refused_confirm_is_a_failure() -> None:
    final = f'data: <template hx-target="#reply-{_MESSAGE_ID}"><p data-draft-id="{_DRAFT}">\n\n'

    result = await _play_stub(
        _stub_app(lambda _: httpx2.Response(200, text=final), confirm_status=409)
    )

    assert result.outcome == "confirm_http_409"
    assert summarize([result]).failures == [1]


def test_the_base_url_defaults_to_the_local_stack_and_can_be_changed() -> None:
    assert parse_args([]).url == "http://localhost:8000"
    assert parse_args(["--url", "http://localhost:9000"]).url == "http://localhost:9000"


def test_the_make_target_runs_the_script_as_a_module_so_its_imports_resolve() -> None:
    root = Path(__file__).parents[2]
    [recipe] = [
        line.strip()
        for line in (root / "Makefile").read_text().splitlines()
        if "scripts.latency_scenario" in line
    ]
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    ran = subprocess.run(
        [sys.executable, "-m", "scripts.latency_scenario", "--help"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert recipe.startswith("uv run python -m scripts.latency_scenario")
    assert ran.returncode == 0, ran.stderr
    assert "--url" in ran.stdout


async def test_a_dropped_reply_stream_is_named_and_the_run_goes_on() -> None:
    def dropped(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.RemoteProtocolError("peer closed connection", request=request)

    result = await _play_stub(_stub_app(dropped))

    assert result.outcome == "transport_RemoteProtocolError"
    assert summarize([result]).failures == [1]


async def test_a_stalled_message_post_is_named_and_the_next_turn_still_runs() -> None:
    posts: list[str] = []
    final = f'data: <template hx-target="#reply-{_MESSAGE_ID}"><p data-draft-id="{_DRAFT}">\n\n'

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/messages":
            posts.append(request.url.path)
            if len(posts) == 1:
                raise httpx2.ReadTimeout("stalled", request=request)
            return httpx2.Response(200, text=f'hx-sse:connect="/messages/{_MESSAGE_ID}/reply"')
        if request.url.path.endswith("/confirm"):
            raise httpx2.ConnectError("refused", request=request)
        return httpx2.Response(200, text=final)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle), base_url=_BASE) as c:
        first, second = await play(
            c,
            FakeLlmCalls(uuid4()),
            uuid4(),
            csrf_token="token",
            turns=_TWO_LOGS_AND_CHITCHAT[:1] * 2,
            timer=SteppingTimer(1),
        )

    assert first.outcome == "transport_ReadTimeout"
    assert second.outcome == "confirm_transport_ConnectError"
    assert summarize([first, second]).failures == [1, 2]


async def test_a_message_refused_with_its_composer_is_named_no_reply() -> None:
    def composer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<form>composer</form>")

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(composer), base_url=_BASE
    ) as client:
        [result] = await play(
            client,
            FakeLlmCalls(uuid4()),
            uuid4(),
            csrf_token="token",
            turns=_TWO_LOGS_AND_CHITCHAT[:1],
            timer=SteppingTimer(1),
        )

    assert result.outcome == "no_reply"
