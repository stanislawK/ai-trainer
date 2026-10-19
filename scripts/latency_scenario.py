"""Replays the latency scenario against the running app and prints p50 and p95 per log turn
(ADR-0026 step 1, PRD-0005 G14, ticket #185). It calls real models, so it costs money.

Run with `make latency-scenario` while `make start` serves the app. It signs in as a seeded
climbing-only athlete of its own, sends ADR-0025's five messages and then eight follow-up logs
in one fresh conversation as a browser would, and confirms each log's draft card except the
7a one, which ADR-0025 leaves pending. A turn is timed from sending the message to the last
event of its reply stream, which carries the draft card. Its `llm_calls` rows are the ones the
athlete gained during the turn, and its USD is their sum: Jev rows carry no cost (ADR-0018).
A reply that ends in a timeout or an error, or a log with no card, is counted and named.

The summary is also written as JSON under `LLM_DEBUG_LOG_DIR`. Like `dev_session.py`, this is a
script, never a route: it reaches the app's database through `DATABASE_URL` to seed the athlete
and read its `llm_calls` rows.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, TextIO
from uuid import UUID

import httpx2

from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.csrf import generate_csrf_token
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort
from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.llm_calls import LlmCall
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.llm.debug_report import percentile
from ai_trainer.settings import Settings
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME
from scripts.dev_session import get_or_create_active_user

DEV_LATENCY_SUB = "dev-latency-scenario"
DEV_LATENCY_EMAIL = "dev-latency-scenario@example.com"
DEFAULT_BASE_URL = "http://localhost:8000"

# Two model calls in series under a 30 s timeout each, plus Jev: a reply that takes longer than
# this is stuck, and the turn is named as a timeout rather than waited on forever.
_REPLY_READ_TIMEOUT_S = 180.0
_HEALTH_TIMEOUT_S = 3.0
# Sending a message or confirming a draft calls no model; it answers at once or is stuck.
_REQUEST_TIMEOUT_S = 30.0
_HTMX = {"HX-Request": "true"}
_REPLY_URL = re.compile(r'hx-sse:connect="/messages/([0-9a-f-]{36})/reply"')
_DRAFT_ID = re.compile(r'data-draft-id="([0-9a-f-]{36})"')


@dataclass(frozen=True, slots=True)
class Turn:
    text: str
    log: bool
    confirm: bool


# ADR-0025's scenario, then a chain of eight follow-up logs in the same conversation (#185).
SCENARIO: tuple[Turn, ...] = (
    Turn("Bouldering at the gym tonight, sent three 6b problems and flashed a 6a", True, True),
    Turn("Log this too: after that I climbed two 6b+ boulders and fell twice on a 6c", True, True),
    Turn("Also log: finished with four 5c traverses as a cool-down", True, True),
    Turn("One more for the log: I tried a 7a project three times without topping", True, False),
    Turn("Thanks, that's all for today!", log=False, confirm=False),
    Turn("Actually I went back in and flashed a 6a+ arete", True, True),
    Turn("Add to the log: two 6c boulders, both sent on the second try", True, True),
    Turn("Also log a crimpy 6b I sent first go", True, True),
    Turn("Log it: fell three times on a 7a+ compression problem", True, True),
    Turn("Then two 5c problems as a warm-down", True, True),
    Turn("One more: a 6b+ dyno, sent on my fourth attempt", True, True),
    Turn("Also log a 6a slab I flashed", True, True),
    Turn("Last one: three laps of a 5b circuit to finish", True, True),
)


class LlmCallsSource(Protocol):
    async def list_for_user(self, user_id: UUID) -> list[LlmCall]: ...


@dataclass(frozen=True, slots=True)
class CallRow:
    template_id: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost: Decimal | None
    outcome: str


@dataclass(frozen=True, slots=True)
class TurnResult:
    number: int
    text: str
    log: bool
    ms: int
    # `ok`, `timeout`, `error`, `no_card` (a log whose reply showed no draft card),
    # `stream_ended` (no final event), `no_reply` (the message was refused with its composer),
    # `http_<status>`, `transport_<error>` (the connection failed), or `confirm_` and one of
    # the last two.
    outcome: str
    calls: tuple[CallRow, ...]
    draft_id: UUID | None = None

    @property
    def usd(self) -> Decimal:
        return sum((call.cost for call in self.calls if call.cost is not None), Decimal(0))


@dataclass(frozen=True, slots=True)
class Summary:
    turns: list[TurnResult]
    log_turns: int
    p50_ms: int | None
    p95_ms: int | None
    timeouts: list[int] = field(default_factory=list)
    failures: list[int] = field(default_factory=list)


class StackDownError(Exception):
    """The app isn't answering, so nothing was seeded or sent."""


async def seed_latency_athlete(
    users: UsersRepositoryPort,
    sessions: SessionsRepositoryPort,
    clock: ClockPort,
    *,
    onboarding: OnboardingRepositoryPort,
    session_ttl: timedelta,
) -> Session:
    """The scenario's athlete: onboarded, climbing only, and nobody else's seeded user, so the
    `llm_calls` rows it gains during a turn are that turn's alone."""
    user = await get_or_create_active_user(users, sub=DEV_LATENCY_SUB, email=DEV_LATENCY_EMAIL)
    await onboarding.replace_sports(user.id, ["climbing"])
    if user.onboarded_at is None:
        await onboarding.set_onboarded_at(user.id, clock.now())
    return await sessions.create(NewSession(user_id=user.id, expires_at=clock.now() + session_ttl))


async def check_stack(client: httpx2.AsyncClient) -> None:
    url = str(client.base_url.join("/health"))
    try:
        response = await client.get("/health", timeout=_HEALTH_TIMEOUT_S)
    except httpx2.TransportError as error:
        raise StackDownError(f"The app isn't answering at {url} ({error}).") from None
    if response.status_code != httpx2.codes.OK:
        raise StackDownError(f"The app at {url} answered {response.status_code}.")
    # `/health` answers 200 with the app up and its database down: that stack can't seed.
    if response.json().get("database") != "ok":
        raise StackDownError(f"The app at {url} says its database is down ({response.text}).")


async def _events(response: httpx2.Response) -> AsyncIterator[str]:
    """Each server-sent event's data, its `data:` lines joined with newlines."""
    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data.append(line.removeprefix("data:").removeprefix(" "))
        elif not line and data:
            yield "\n".join(data)
            data = []
    if data:
        yield "\n".join(data)


async def _reply(client: httpx2.AsyncClient, message_id: str) -> tuple[str, UUID | None]:
    """How the reply ended, and the draft card it showed, if any. The final event targets the
    whole reply (`#reply-<id>`); phases and chunks target its parts."""
    final = f'hx-target="#reply-{message_id}"'
    try:
        async with client.stream(
            "GET", f"/messages/{message_id}/reply", timeout=_REPLY_READ_TIMEOUT_S
        ) as response:
            if response.status_code != httpx2.codes.OK:
                return f"http_{response.status_code}", None
            async for event in _events(response):
                if final not in event:
                    continue
                if f"/messages/{message_id}/retry" in event:
                    return ("timeout" if "took too long" in event else "error"), None
                card = _DRAFT_ID.search(event)
                return "ok", UUID(card.group(1)) if card else None
    except httpx2.TimeoutException:
        return "timeout", None
    return "stream_ended", None


def _row(call: LlmCall) -> CallRow:
    return CallRow(
        template_id=call.template_id,
        model=call.model,
        latency_ms=call.latency_ms,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cost=call.cost,
        outcome=call.outcome.value,
    )


async def play(
    client: httpx2.AsyncClient,
    calls: LlmCallsSource,
    user_id: UUID,
    *,
    csrf_token: str,
    turns: Sequence[Turn] = SCENARIO,
    timer: Callable[[], float] = time.perf_counter,
) -> list[TurnResult]:
    """Sends `turns` one after another in one fresh conversation, as htmx does."""
    headers = {**_HTMX, CSRF_HEADER_NAME: csrf_token}
    conversation_id = ""
    seen = {call.id for call in await calls.list_for_user(user_id)}
    results: list[TurnResult] = []
    for number, turn in enumerate(turns, start=1):
        started = timer()
        draft_id: UUID | None = None
        try:
            sent = await client.post(
                "/messages",
                data={"message": turn.text, "conversation_id": conversation_id},
                headers=headers,
            )
            if sent.status_code != httpx2.codes.OK:
                outcome = f"http_{sent.status_code}"
            else:
                conversation_id = sent.headers.get("HX-Replace-Url", "").removeprefix("/c/") or (
                    conversation_id
                )
                match = _REPLY_URL.search(sent.text)
                outcome, draft_id = (
                    await _reply(client, match.group(1)) if match else ("no_reply", None)
                )
        except httpx2.HTTPError as error:
            # A dropped connection or a stalled request ends this turn, never the run.
            outcome = f"transport_{type(error).__name__}"
        ms = round((timer() - started) * 1000)
        if outcome == "ok" and turn.log and draft_id is None:
            outcome = "no_card"
        if outcome == "ok" and turn.confirm and draft_id is not None:
            try:
                confirmed = await client.post(f"/drafts/{draft_id}/confirm", headers=headers)
            except httpx2.HTTPError as error:
                outcome = f"confirm_transport_{type(error).__name__}"
            else:
                if confirmed.status_code != httpx2.codes.OK:
                    outcome = f"confirm_http_{confirmed.status_code}"
        new = [call for call in await calls.list_for_user(user_id) if call.id not in seen]
        seen.update(call.id for call in new)
        new.sort(key=lambda call: call.created_at)
        results.append(
            TurnResult(
                number=number,
                text=turn.text,
                log=turn.log,
                ms=ms,
                outcome=outcome,
                calls=tuple(_row(call) for call in new),
                draft_id=draft_id,
            )
        )
    return results


def summarize(results: Sequence[TurnResult]) -> Summary:
    """p50 and p95 by nearest rank over every log turn, failed ones included: a timed-out log
    is as slow for the athlete as it took (G14)."""
    log_ms = [result.ms for result in results if result.log]
    return Summary(
        turns=list(results),
        log_turns=len(log_ms),
        p50_ms=percentile(log_ms, 50) if log_ms else None,
        p95_ms=percentile(log_ms, 95) if log_ms else None,
        timeouts=[result.number for result in results if result.outcome == "timeout"],
        failures=[result.number for result in results if result.outcome not in {"ok", "timeout"}],
    )


def _seconds(ms: int | None) -> str:
    return "n/a" if ms is None else f"{ms / 1000:.1f} s"


def render(summary: Summary) -> str:
    lines = [f"{'#':>2}  {'kind':<4}  {'outcome':<12}  {'send→card':>9}  {'USD':>9}  message"]
    for turn in summary.turns:
        kind = "log" if turn.log else "chat"
        lines.append(
            f"{turn.number:>2}  {kind:<4}  {turn.outcome:<12}  {turn.ms / 1000:>7.1f} s"
            f"  {turn.usd:>9.6f}  {turn.text}"
        )
        lines.extend(
            f"{'':>6}{call.template_id} {call.latency_ms} ms "
            f"{call.input_tokens}→{call.output_tokens} tok {call.outcome}"
            for call in turn.calls
        )
    lines.append(
        f"Log turns: {summary.log_turns} · p50 {_seconds(summary.p50_ms)}"
        f" · p95 {_seconds(summary.p95_ms)} · timeouts {len(summary.timeouts)}"
        f" · failures {len(summary.failures)}"
    )
    named = sorted([*summary.timeouts, *summary.failures])
    lines.extend(
        f"  #{turn.number} {turn.outcome}: {turn.text}"
        for turn in summary.turns
        if turn.number in named
    )
    return "\n".join(lines) + "\n"


def write_json(summary: Summary, directory: Path, *, now: datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"latency-scenario-{now:%Y%m%dT%H%M%SZ}.json"
    body = asdict(summary)
    for turn, result in zip(body["turns"], summary.turns, strict=True):
        turn["usd"] = result.usd
    path.write_text(json.dumps(body, default=str, indent=2) + "\n")
    return path


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help="the running app's base URL")
    return parser.parse_args(argv)


async def main(
    *,
    base_url: str = DEFAULT_BASE_URL,
    output: TextIO = sys.stdout,
    errors: TextIO = sys.stderr,
    settings: Settings | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    turns: Sequence[Turn] = SCENARIO,
) -> int:
    """Checks the stack before anything else, so a stopped app costs no seeding and no model
    call."""
    async with httpx2.AsyncClient(
        base_url=base_url, transport=transport, timeout=_REQUEST_TIMEOUT_S
    ) as client:
        try:
            await check_stack(client)
        except StackDownError as error:
            errors.write(f"{error} Start the stack with `make start` first; nothing was sent.\n")
            return 1
        settings = settings if settings is not None else Settings()
        clock = UtcClock()
        engine = build_engine(str(settings.database_url))
        try:
            session_factory = build_session_factory(engine)
            session = await seed_latency_athlete(
                SqlAlchemyUsersRepository(session_factory),
                SqlAlchemySessionsRepository(session_factory),
                clock,
                onboarding=SqlAlchemyOnboardingRepository(session_factory),
                session_ttl=timedelta(days=settings.session_ttl_days),
            )
            client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
            csrf_token = generate_csrf_token(
                session.id, settings.csrf_secret_key.get_secret_value().encode()
            )
            results = await play(
                client,
                SqlAlchemyLlmCallsRepository(session_factory),
                session.user_id,
                csrf_token=csrf_token,
                turns=turns,
            )
        finally:
            await engine.dispose()
    summary = summarize(results)
    output.write(render(summary))
    path = write_json(summary, settings.llm_debug_log_dir, now=clock.now())
    output.write(f"Summary JSON: {path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(
        asyncio.run(main(base_url=parse_args(sys.argv[1:]).url))
    )  # pragma: no cover -- only runs via direct script execution, not import
