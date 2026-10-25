"""What a span may carry out of the process (ADR-0018 invariant 4, ADR-0030 invariant 3)."""

from collections.abc import Sequence

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind, Status

# The HTTP instrumentations record the client's address and user agent on every server span,
# under the old and the stable semantic conventions alike.
_DROPPED = frozenset(
    {
        "url.query",
        "client.address",
        "client.port",
        "net.peer.ip",
        "net.peer.port",
        "net.sock.peer.addr",
        "net.sock.peer.port",
        "http.client_ip",
        "http.user_agent",
        "user_agent.original",
        "user_agent.synthetic.type",
    }
)
# `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*` would record headers, the session cookie among them.
_DROPPED_PREFIXES = ("http.request.header.", "http.response.header.")
# A request's path can name a user (`/admin/users/{user_id}/status`), so its span keeps only
# `http.route`; an outbound call keeps its URL, cut at the query, which may hold a user's words.
_URLS = frozenset({"http.url", "url.full", "http.target", "url.path"})
# Driver and SQLAlchemy errors quote the values and parameters they were given.
_EXCEPTION_KEPT = frozenset({"exception.type", "exception.escaped"})


class ScrubbingSpanExporter(SpanExporter):
    """Drops client, path, query and error-text attributes from each span before `exporter`
    sends it."""

    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._exporter.export([_scrubbed(span) for span in spans])

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)


def _scrubbed(span: ReadableSpan) -> ReadableSpan:
    server = span.kind is SpanKind.SERVER
    attributes = {
        key: _without_query(value) if key in _URLS and isinstance(value, str) else value
        for key, value in (span.attributes or {}).items()
        if key not in _DROPPED
        and not key.startswith(_DROPPED_PREFIXES)
        and not (server and key in _URLS)
    }
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=[_scrubbed_event(event) for event in span.events],
        links=span.links,
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def _scrubbed_event(event: Event) -> Event:
    if event.name != "exception":
        return event
    kept = {k: v for k, v in (event.attributes or {}).items() if k in _EXCEPTION_KEPT}
    return Event(event.name, kept, event.timestamp)


def _without_query(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]
