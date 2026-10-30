"""Every request traced end to end through the real composition root (ADR-0030, #271): the
app's own middleware, PostgreSQL and model gateways, with spans kept in memory instead of
exported. OpenRouter answers from `httpcore2`'s pool, below the instrumented `httpx2`
transport, so the real client path is traced and no socket opens. Its answer is a refusal,
so a reply is one routing attempt and a friendly error, never a real model call."""

import json
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from uuid import uuid4

import httpcore2
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from pydantic_ai import models
from sqlalchemy import text

from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.main import create_app
from ai_trainer.settings import Settings
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME

UNREACHABLE_ENDPOINT = "http://127.0.0.1:1"
MESSAGE = "my left knee hurt on the 7a crux"
QUERY_VALUE = "private-query-value"
_CSRF_TOKEN_IN_HTML = re.compile(r'hx-headers:inherited=\'\{"X-CSRF-Token": "([0-9a-f]+)"\}\'')
_HTMX = {"HX-Request": "true"}
# The app reads the real clock, so the session must outlive any run.
_FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


@pytest.fixture
def openrouter_refuses(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every outbound request gets a 400, which no SDK retries; yields the URLs asked for."""
    asked: list[str] = []

    async def refuse(
        self: httpcore2.AsyncConnectionPool, request: httpcore2.Request
    ) -> httpcore2.Response:
        asked.append(f"{request.url.scheme.decode()}://{request.url.host.decode()}")
        body = json.dumps({"error": {"message": "refused in tests", "code": 400}}).encode()
        return httpcore2.Response(
            400, headers=[(b"content-type", b"application/json")], content=body
        )

    monkeypatch.setattr(httpcore2.AsyncConnectionPool, "handle_async_request", refuse)
    with models.override_allow_model_requests(True):
        yield asked


@pytest.fixture
async def athlete() -> AsyncIterator[tuple[User, str]]:
    """An onboarded, active athlete with a live session, committed because `create_app` opens
    its own engine; deleting the user at teardown cascades to everything the test wrote."""
    engine = build_engine(str(Settings().database_url))
    session_factory = build_session_factory(engine)
    users = SqlAlchemyUsersRepository(session_factory)
    sub = f"tracing-{uuid4()}"
    user = await users.create(
        NewUser(
            sub=sub,
            email=f"{sub}@example.com",
            name="Ada Tracing",
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE users SET onboarded_at = :at WHERE id = :id"),
            {"at": datetime(2026, 10, 1, tzinfo=UTC), "id": user.id},
        )
    session = await SqlAlchemySessionsRepository(session_factory).create(
        NewSession(user_id=user.id, expires_at=_FAR_FUTURE)
    )
    try:
        yield user, str(session.id)
    finally:
        await users.delete(user.id)
        await engine.dispose()


def _traced_client(exporter: InMemorySpanExporter, session_id: str) -> TestClient:
    settings = Settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT)
    client = TestClient(
        create_app(settings, span_exporter=exporter, metric_reader=InMemoryMetricReader())
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    return client


def _send_and_read_reply(
    client: TestClient, exporter: InMemorySpanExporter, *, query: str = ""
) -> tuple[str, str]:
    """Sends `MESSAGE` and reads its reply stream; returns both requests' trace IDs."""
    page = client.get("/")
    token = _CSRF_TOKEN_IN_HTML.search(page.text)
    assert token is not None, page.text
    sent = client.post(
        f"/messages{query}",
        data={"message": MESSAGE},
        headers={CSRF_HEADER_NAME: token.group(1), **_HTMX},
    )
    assert sent.status_code == 200, sent.text
    message_id = re.search(r"/messages/([0-9a-f-]{36})/reply", sent.text)
    assert message_id is not None, sent.text
    reply = client.get(f"/messages/{message_id.group(1)}/reply{query}")
    assert reply.status_code == 200
    spans = _finished(exporter)
    return _server_trace(spans, "POST", "/messages"), _server_trace(
        spans, "GET", "/messages/{message_id}/reply"
    )


def _finished(exporter: InMemorySpanExporter) -> Sequence[ReadableSpan]:
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider.force_flush(timeout_millis=2000)
    return exporter.get_finished_spans()


def _server_trace(spans: Sequence[ReadableSpan], method: str, route: str) -> str:
    (span,) = [
        span
        for span in spans
        if span.kind is SpanKind.SERVER
        and span.attributes
        and span.attributes.get("http.route") == route
        and span.name.startswith(method)
    ]
    assert span.context is not None
    return format(span.context.trace_id, "032x")


def _in_trace(spans: Sequence[ReadableSpan], trace_id: str) -> list[ReadableSpan]:
    return [s for s in spans if s.context and format(s.context.trace_id, "032x") == trace_id]


def _ancestors(span: ReadableSpan, spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
    by_id = {s.context.span_id: s for s in spans if s.context}
    chain: list[ReadableSpan] = []
    parent = span.parent
    while parent is not None and parent.span_id in by_id:
        chain.append(by_id[parent.span_id])
        parent = by_id[parent.span_id].parent
    return chain


def _url(span: ReadableSpan) -> str:
    attributes = span.attributes or {}
    return str(attributes.get("url.full", attributes.get("http.url", "")))


async def test_a_chat_turn_traces_the_request_its_queries_the_agent_and_the_openrouter_call(
    athlete: tuple[User, str], openrouter_refuses: list[str]
) -> None:
    """AC1. The POST stores the message; its reply stream, a request of its own, calls the
    models. Each is one trace rooted at its request span."""
    exporter = InMemorySpanExporter()
    client = _traced_client(exporter, athlete[1])

    posted, replied = _send_and_read_reply(client, exporter)
    spans = _finished(exporter)

    for trace_id in (posted, replied):
        in_trace = _in_trace(spans, trace_id)
        (root,) = [s for s in in_trace if s.parent is None]
        assert root.kind is SpanKind.SERVER
        database = [s for s in in_trace if s.attributes and s.attributes.get("db.system")]
        assert database, f"no database span in {[s.name for s in in_trace]}"
        assert all(root in _ancestors(s, in_trace) for s in database)

    in_reply = _in_trace(spans, replied)
    # Pydantic AI's model-request spans are CLIENT spans too; httpx2's carry the URL.
    calls = [s for s in in_reply if s.kind is SpanKind.CLIENT and _url(s)]
    assert len(calls) == len(openrouter_refuses) > 0, [s.name for s in in_reply]
    assert set(openrouter_refuses) == {"https://openrouter.ai"}
    for call in calls:
        assert _url(call).startswith("https://openrouter.ai/api/v1/"), _url(call)
        chain = _ancestors(call, in_reply)
        assert any(s.name.startswith("invoke_agent") for s in chain), [s.name for s in chain]
        assert chain[-1].kind is SpanKind.SERVER


async def test_no_span_carries_the_user_their_email_their_message_or_a_query_value(
    athlete: tuple[User, str], openrouter_refuses: list[str]
) -> None:
    """AC3 (ADR-0018 invariant 4, ADR-0030 invariant 3)."""
    exporter = InMemorySpanExporter()
    client = _traced_client(exporter, athlete[1])
    user = athlete[0]

    _send_and_read_reply(client, exporter, query=f"?q={QUERY_VALUE}")
    spans = _finished(exporter)

    assert spans
    forbidden = (str(user.id), user.id.hex, user.email, MESSAGE, QUERY_VALUE, "testclient")
    for span in spans:
        exported = json.dumps(
            [
                span.name,
                span.status.description,
                dict(span.attributes or {}),
                [dict(e.attributes or {}) for e in span.events],
                dict(span.resource.attributes),
            ],
            default=str,
        )
        for value in forbidden:
            assert value not in exported, f"{value!r} in span {span.name!r}: {exported}"
