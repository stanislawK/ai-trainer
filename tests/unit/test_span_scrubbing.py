"""What leaves the process in a span (ADR-0018 invariant 4, ADR-0030 invariant 3, #271): no
query string, request path, client address, user agent or error text, whichever HTTP semantic
conventions set them."""

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from ai_trainer.adapters.telemetry import ScrubbingSpanExporter


def _scrubbed(span: ReadableSpan) -> ReadableSpan:
    inner = InMemorySpanExporter()
    result = ScrubbingSpanExporter(inner).export([span])

    assert result is SpanExportResult.SUCCESS
    (exported,) = inner.get_finished_spans()
    return exported


def _exported(
    attributes: dict[str, str | int], kind: SpanKind = SpanKind.INTERNAL
) -> dict[str, object]:
    span = _scrubbed(ReadableSpan("GET /c/{id}", attributes=attributes, kind=kind))
    return dict(span.attributes or {})


def test_a_request_span_keeps_its_route_template_but_not_its_path() -> None:
    """A path can name a user, as `/admin/users/{user_id}/status` does."""
    exported = _exported(
        {
            "http.url": "http://testserver/admin/users/0b5e/status?q=my+knee",
            "url.full": "http://testserver/admin/users/0b5e/status",
            "http.target": "/admin/users/0b5e/status",
            "url.path": "/admin/users/0b5e/status",
            "http.route": "/admin/users/{user_id}/status",
        },
        kind=SpanKind.SERVER,
    )

    assert exported == {"http.route": "/admin/users/{user_id}/status"}


def test_an_outbound_call_keeps_its_url_without_the_query() -> None:
    exported = _exported(
        {
            "http.url": "https://openrouter.ai/api/v1/chat/completions?q=1",
            "url.full": "https://openrouter.ai/api/v1/chat/completions?q=1#frag",
        },
        kind=SpanKind.CLIENT,
    )

    assert exported == {
        "http.url": "https://openrouter.ai/api/v1/chat/completions",
        "url.full": "https://openrouter.ai/api/v1/chat/completions",
    }


def test_an_error_keeps_its_status_code_and_exception_type_but_not_their_text() -> None:
    """Driver errors quote the values they rejected, and SQLAlchemy's quote the parameters."""
    leak = "Key (email)=(ada@example.com) already exists"
    span = _scrubbed(
        ReadableSpan(
            "INSERT ai_trainer",
            status=Status(StatusCode.ERROR, leak),
            events=[
                Event(
                    "exception",
                    {
                        "exception.type": "IntegrityError",
                        "exception.message": leak,
                        "exception.stacktrace": f"Traceback ... {leak}",
                        "exception.escaped": "False",
                    },
                ),
                Event("retry", {"attempt": 2}),
            ],
        )
    )

    assert (span.status.status_code, span.status.description) == (StatusCode.ERROR, None)
    assert [(event.name, dict(event.attributes or {})) for event in span.events] == [
        ("exception", {"exception.type": "IntegrityError", "exception.escaped": "False"}),
        ("retry", {"attempt": 2}),
    ]


def test_query_client_address_and_user_agent_attributes_are_dropped() -> None:
    exported = _exported(
        {
            "url.query": "q=my+knee",
            "client.address": "203.0.113.7",
            "client.port": 51234,
            "net.peer.ip": "203.0.113.7",
            "net.peer.port": 51234,
            "http.client_ip": "203.0.113.7",
            "http.user_agent": "Mozilla/5.0",
            "user_agent.original": "Mozilla/5.0",
            "http.route": "/c/{conversation_id}",
            "http.status_code": 200,
        }
    )

    assert exported == {"http.route": "/c/{conversation_id}", "http.status_code": 200}


def test_a_span_keeps_its_name_ids_and_timing() -> None:
    inner = InMemorySpanExporter()
    original = ReadableSpan("GET /", attributes={"url.query": "x=1"}, start_time=1, end_time=2)

    ScrubbingSpanExporter(inner).export([original])

    (span,) = inner.get_finished_spans()
    assert (span.name, span.context, span.start_time, span.end_time) == ("GET /", None, 1, 2)


def test_flush_and_shutdown_reach_the_wrapped_exporter() -> None:
    inner = InMemorySpanExporter()
    exporter = ScrubbingSpanExporter(inner)

    assert exporter.force_flush(timeout_millis=10) is True
    exporter.shutdown()

    assert exporter.export([ReadableSpan("late")]) is SpanExportResult.FAILURE


def test_captured_headers_are_dropped_even_when_an_env_var_turns_capture_on() -> None:
    exported = _exported(
        {
            "http.request.header.cookie": "session_id=0b5e",
            "http.response.header.set_cookie": "session_id=0b5e",
            "http.method": "GET",
        }
    )

    assert exported == {"http.method": "GET"}
