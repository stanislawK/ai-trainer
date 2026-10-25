"""What a log line may carry out of the process (ADR-0030 invariant 3).

One logging config for the app and Uvicorn: JSON lines in production, plain text by default,
each with the current span's `trace_id` and `span_id`, the request's `request_id` and the
signed-in athlete's opaque `user_id`.
"""

import json
import logging
import logging.config
import sys
import traceback
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TextIO
from uuid import UUID

from opentelemetry import trace

from ai_trainer.settings import Settings


@dataclass
class _RequestLogContext:
    request_id: str
    # Filled in by the sign-in gate, inside the request, once it has resolved the athlete.
    user_id: str | None = None


_request: ContextVar[_RequestLogContext | None] = ContextVar("request_log_context", default=None)

# The access line's fields; any other `extra` a caller passes is never written.
_ACCESS_FIELDS = ("method", "route", "status", "duration_ms")
_CORRELATION_FIELDS = ("trace_id", "span_id", "request_id", "user_id")


def bind_request(request_id: str) -> Token[_RequestLogContext | None]:
    return _request.set(_RequestLogContext(request_id))


def unbind_request(token: Token[_RequestLogContext | None]) -> None:
    _request.reset(token)


def bind_user_id(user_id: UUID) -> None:
    context = _request.get()
    if context is not None:
        context.user_id = str(user_id)


class CorrelationFilter(logging.Filter):
    """Adds the correlation IDs that exist; one that doesn't is left off, never `0`."""

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            record.trace_id = format(span.trace_id, "032x")
            record.span_id = format(span.span_id, "016x")
        context = _request.get()
        if context is not None:
            record.request_id = context.request_id
            if context.user_id is not None:
                record.user_id = context.user_id
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="microseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in (*_CORRELATION_FIELDS, *_ACCESS_FIELDS):
            if field in record.__dict__:
                line[field] = record.__dict__[field]
        if record.exc_info and record.exc_info[0] is not None:
            # The type and the frames only: driver and SQLAlchemy errors quote the values and
            # parameters they were given, as `ScrubbingSpanExporter` notes for spans.
            line["exc_type"] = record.exc_info[0].__name__
            line["stack"] = "".join(traceback.format_tb(record.exc_info[2]))
        return json.dumps(line, default=str)


_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(settings: Settings, *, stream: TextIO = sys.stdout) -> None:
    """Routes the app's and Uvicorn's loggers to one handler on `stream`.

    Uvicorn's own access line is turned off in both formats: it writes the client address and
    the raw path with its query. The app's request middleware writes the access line instead.
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"correlation": {"()": CorrelationFilter}},
            "formatters": {
                "json": {"()": JsonFormatter},
                "text": {"format": _TEXT_FORMAT},
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": stream,
                    "formatter": settings.log_format,
                    "filters": ["correlation"],
                }
            },
            "root": {"level": settings.log_level, "handlers": ["stdout"]},
            "loggers": {
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                # Uvicorn writes an access line only when this logger `hasHandlers()`.
                "uvicorn.access": {"handlers": [], "propagate": False},
                # httpx logs each outbound URL with its query at INFO; outbound calls are in
                # the trace already, and a lookup's query can hold an athlete's words.
                "httpx": {"level": "WARNING"},
                "httpx2": {"level": "WARNING"},
            },
        }
    )
