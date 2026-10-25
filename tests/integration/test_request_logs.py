"""JSON request logs through the real composition root (ADR-0030, #270): every line a request
writes carries its `request_id`, its trace and span when traced, and the signed-in athlete's
opaque `user_id`, and never their email, their words or their address."""

import asyncio
import io
import json
import logging
import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from starlette.types import Message

from ai_trainer.adapters.log_records import configure_logging
from ai_trainer.domain.users import User
from ai_trainer.main import create_app
from ai_trainer.settings import Settings
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from tests.integration.test_request_tracing import (
    MESSAGE,
    QUERY_VALUE,
    UNREACHABLE_ENDPOINT,
    _finished,
    _send_and_read_reply,
    athlete,
    openrouter_refuses,
)

__all__ = ["athlete", "openrouter_refuses"]

_REQUEST_ID = "X-Request-ID"
_NEW_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_ACCESS = "ai_trainer.access"
# A sign-in cancelled on Google's consent screen: the callback logs a warning, no network.
_CANCELLED_SIGN_IN = "/auth/callback?error=access_denied"
_CLIENT_IP = "198.51.100.4"
_FORWARDED_IP = "203.0.113.7"
_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def log_lines() -> Iterator[io.StringIO]:
    """Logs configured as in production, into a buffer; restored after the test."""
    saved = [
        (logger, logger.handlers[:], logger.level, logger.propagate)
        for logger in (logging.getLogger(name) for name in _LOGGERS)
    ]
    stream = io.StringIO()
    configure_logging(Settings(log_format="json", log_level="INFO"), stream=stream)
    yield stream
    for logger, handlers, level, propagate in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _parsed(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_every_line_of_a_request_is_json_with_its_request_id(log_lines: io.StringIO) -> None:
    """AC1."""
    client = TestClient(create_app(Settings()))

    first = client.get(_CANCELLED_SIGN_IN, follow_redirects=False)
    second = client.get(_CANCELLED_SIGN_IN, follow_redirects=False)

    lines = _parsed(log_lines)
    ids = [first.headers[_REQUEST_ID], second.headers[_REQUEST_ID]]
    assert ids[0] != ids[1]
    for request_id in ids:
        mine = [line for line in lines if line.get("request_id") == request_id]
        assert [(line["logger"], line["level"]) for line in mine] == [
            ("ai_trainer.web.auth", "WARNING"),
            (_ACCESS, "INFO"),
        ]
    assert len(lines) == 4


def test_the_access_line_has_method_route_template_status_and_duration(
    log_lines: io.StringIO,
) -> None:
    client = TestClient(create_app(Settings()))

    client.get("/messages/00000000-0000-0000-0000-000000000000/reply?q=1")

    (access,) = [line for line in _parsed(log_lines) if line["logger"] == _ACCESS]
    assert access["method"] == "GET"
    assert access["route"] == "/messages/{message_id}/reply"
    assert access["status"] == 401
    assert isinstance(access["duration_ms"], float)
    assert "00000000" not in json.dumps(access)


def test_an_unmatched_path_logs_no_route(log_lines: io.StringIO) -> None:
    """A raw path can carry anything; only a route template is ever written."""
    client = TestClient(create_app(Settings()))

    client.get("/sign-in/ada@example.com")

    (access,) = [line for line in _parsed(log_lines) if line["logger"] == _ACCESS]
    assert access["route"] is None
    assert "ada@example.com" not in log_lines.getvalue()


def test_trace_ids_match_the_request_span_and_are_absent_untraced(
    log_lines: io.StringIO,
) -> None:
    """AC2."""
    exporter = InMemorySpanExporter()
    traced = TestClient(
        create_app(
            Settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT),
            span_exporter=exporter,
        )
    )

    request_id = traced.get(_CANCELLED_SIGN_IN, follow_redirects=False).headers[_REQUEST_ID]
    spans = _finished(exporter)

    (server,) = [s for s in spans if s.kind is SpanKind.SERVER]
    assert server.context is not None
    lines = [line for line in _parsed(log_lines) if line["request_id"] == request_id]
    assert len(lines) == 2
    span_ids = {format(s.context.span_id, "016x") for s in spans if s.context}
    for line in lines:
        assert line["trace_id"] == format(server.context.trace_id, "032x")
        assert line["span_id"] in span_ids


def test_an_untraced_request_logs_no_trace_or_span_id(log_lines: io.StringIO) -> None:
    """AC2: absent, not `0`."""
    client = TestClient(create_app(Settings()))

    client.get(_CANCELLED_SIGN_IN, follow_redirects=False)

    lines = _parsed(log_lines)
    assert lines
    assert all("trace_id" not in line and "span_id" not in line for line in lines)


async def test_a_signed_in_request_logs_the_user_id_but_no_email_message_or_client_ip(
    log_lines: io.StringIO, athlete: tuple[User, str], openrouter_refuses: list[str]
) -> None:
    """AC3: the turn's POST and reply stream, sent from one address and forwarded for another,
    with a query value."""
    user, session_id = athlete
    exporter = InMemorySpanExporter()
    settings = Settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT)
    client = TestClient(create_app(settings, span_exporter=exporter), client=(_CLIENT_IP, 50000))
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    client.headers["X-Forwarded-For"] = _FORWARDED_IP

    _send_and_read_reply(client, exporter, query=f"?q={QUERY_VALUE}")

    lines = _parsed(log_lines)
    access = [line for line in lines if line["logger"] == _ACCESS]
    assert [line["route"] for line in access] == ["/", "/messages", "/messages/{message_id}/reply"]
    assert all(line["user_id"] == str(user.id) for line in lines)
    written = log_lines.getvalue()
    for value in (user.email, MESSAGE, QUERY_VALUE, _CLIENT_IP, _FORWARDED_IP, "testclient"):
        assert value not in written, f"{value!r} in {written}"


def test_health_writes_no_access_line(log_lines: io.StringIO) -> None:
    """AC4: the container probe calls it every few seconds (ADR-0023)."""
    client = TestClient(create_app(Settings()))

    responses = [client.get("/health"), client.get("/health/", follow_redirects=False)]

    assert all(_REQUEST_ID in r.headers for r in responses)
    assert not [line for line in _parsed(log_lines) if line["logger"] == _ACCESS]


@pytest.mark.parametrize(
    "incoming",
    ["a" * 65, "abc def", "abc\tdef", "<script>alert(1)</script>", "", "id;rm -rf /", "zażółć"],
)
def test_a_malformed_or_oversized_request_id_is_replaced(
    log_lines: io.StringIO, incoming: str
) -> None:
    """AC5."""
    client = TestClient(create_app(Settings()))

    response = client.get(
        _CANCELLED_SIGN_IN,
        headers={_REQUEST_ID.encode(): incoming.encode()},
        follow_redirects=False,
    )

    returned = response.headers[_REQUEST_ID]
    assert _NEW_REQUEST_ID.fullmatch(returned)
    assert {line["request_id"] for line in _parsed(log_lines)} == {returned}


@pytest.mark.parametrize("incoming", ["0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "a" * 64, "r._-1"])
def test_a_valid_request_id_is_kept_and_returned(log_lines: io.StringIO, incoming: str) -> None:
    client = TestClient(create_app(Settings()))

    response = client.get(
        _CANCELLED_SIGN_IN, headers={_REQUEST_ID: incoming}, follow_redirects=False
    )

    assert response.headers[_REQUEST_ID] == incoming
    assert {line["request_id"] for line in _parsed(log_lines)} == {incoming}


def test_a_refused_host_still_gets_a_request_id_and_an_access_line(
    log_lines: io.StringIO,
) -> None:
    client = TestClient(create_app(Settings()), base_url="http://evil.example")

    response = client.get("/")

    assert response.status_code == 400
    (access,) = _parsed(log_lines)
    assert access["request_id"] == response.headers[_REQUEST_ID]
    assert access["status"] == 400


def _app_with_a_failing_route() -> FastAPI:
    """A route that raises, under `/static/` so the sign-in gate lets it through."""
    app = create_app(Settings())

    async def boom() -> None:
        # Built at run time, as an error's text would be; the logged frames quote the source.
        raise RuntimeError("".join(["kab", "oom"]))

    app.add_api_route("/static/boom", boom)
    app.router.routes.insert(0, app.router.routes.pop())
    return app


def test_an_unhandled_error_returns_a_500_with_its_request_id_and_logs_it(
    log_lines: io.StringIO,
) -> None:
    """Skeptic challenge on AC1: Starlette's error middleware sends the 500 itself."""
    client = TestClient(_app_with_a_failing_route(), raise_server_exceptions=False)

    response = client.get("/static/boom")

    assert response.status_code == 500
    (access,) = [line for line in _parsed(log_lines) if line["logger"] == _ACCESS]
    assert access["status"] == 500
    assert access["request_id"] == response.headers[_REQUEST_ID]


async def test_the_servers_error_line_for_an_unhandled_error_carries_the_request_id(
    log_lines: io.StringIO,
) -> None:
    """Uvicorn logs an escaped error on `uvicorn.error` in the request's own task, after the
    app returns; this drives the app as Uvicorn's `run_asgi` does."""
    app = _app_with_a_failing_route()
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    async def run_asgi() -> None:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/static/boom",
            "raw_path": b"/static/boom",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
        try:
            await app(scope, receive, send)
        except RuntimeError as error:
            logging.getLogger("uvicorn.error").error(
                "Exception in ASGI application\n", exc_info=error
            )

    await asyncio.create_task(run_asgi())

    errors = [line for line in _parsed(log_lines) if line["logger"] == "uvicorn.error"]
    (error,) = errors
    assert error["exc_type"] == "RuntimeError"
    (access,) = [line for line in _parsed(log_lines) if line["logger"] == _ACCESS]
    assert error["request_id"] == access["request_id"]
    assert "kaboom" not in log_lines.getvalue()
