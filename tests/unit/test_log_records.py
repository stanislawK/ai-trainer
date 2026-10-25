"""Log lines as JSON with correlation IDs (ADR-0030, #270)."""

import io
import json
import logging
import sys
from collections.abc import Iterator
from types import TracebackType
from uuid import UUID

import pytest
from opentelemetry.sdk.trace import TracerProvider

from ai_trainer.adapters.log_records import (
    CorrelationFilter,
    JsonFormatter,
    bind_request,
    bind_user_id,
    configure_logging,
    unbind_request,
)
from ai_trainer.settings import Settings

USER_ID = UUID("0b5f8f8e-7a52-4c38-9d6b-2f1f6f3b8c11")
_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """`configure_logging` changes process-wide loggers; each test leaves them as it found them."""
    saved = [
        (logger, logger.handlers[:], logger.level, logger.propagate)
        for logger in (logging.getLogger(name) for name in _LOGGERS)
    ]
    yield
    for logger, handlers, level, propagate in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _record(message: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("ai_trainer.test", logging.INFO, __file__, 1, message, None, None)
    record.__dict__.update(extra)
    return record


def _formatted(record: logging.LogRecord) -> dict[str, object]:
    CorrelationFilter().filter(record)
    line = json.loads(JsonFormatter().format(record))
    assert isinstance(line, dict)
    return line


def test_a_json_line_has_utc_timestamp_level_logger_and_message() -> None:
    record = _record("draft %s not shown", created=0.0)
    record.args = ("abc",)

    line = _formatted(record)

    assert line == {
        "timestamp": "1970-01-01T00:00:00.000000+00:00",
        "level": "INFO",
        "logger": "ai_trainer.test",
        "message": "draft abc not shown",
    }


def test_a_log_record_outside_a_span_and_a_request_has_no_correlation_ids() -> None:
    line = _formatted(_record())

    assert not {"trace_id", "span_id", "request_id", "user_id"} & line.keys()


def test_a_log_record_inside_a_span_carries_its_trace_and_span_ids() -> None:
    tracer = TracerProvider().get_tracer("test")

    with tracer.start_as_current_span("work") as span:
        line = _formatted(_record())

    context = span.get_span_context()
    assert line["trace_id"] == format(context.trace_id, "032x")
    assert line["span_id"] == format(context.span_id, "016x")


def test_a_log_record_inside_a_request_carries_its_request_and_user_ids() -> None:
    token = bind_request("req-1")
    try:
        anonymous = _formatted(_record())
        bind_user_id(USER_ID)
        signed_in = _formatted(_record())
    finally:
        unbind_request(token)
    after = _formatted(_record())

    assert anonymous["request_id"] == "req-1"
    assert "user_id" not in anonymous
    assert signed_in["user_id"] == str(USER_ID)
    assert "request_id" not in after


def test_binding_a_user_outside_a_request_is_a_no_op() -> None:
    bind_user_id(USER_ID)

    assert "user_id" not in _formatted(_record())


def test_only_the_access_fields_of_a_records_extras_are_written() -> None:
    line = _formatted(
        _record(method="GET", route="/x/{id}", status=200, duration_ms=1.5, email="a@b.c")
    )

    assert line["method"] == "GET"
    assert line["route"] == "/x/{id}"
    assert line["status"] == 200
    assert line["duration_ms"] == 1.5
    assert "email" not in line


def test_an_exception_writes_its_type_and_frames_but_never_its_message() -> None:
    # Built at run time, as a driver error's text would be; the frames quote only the source.
    secret = "".join(["hunter", "2"])
    try:
        raise ValueError(f"password={secret}")
    except ValueError:
        record = logging.LogRecord(
            "ai_trainer.test", logging.ERROR, __file__, 1, "read failed", None, _exc_info()
        )

    line = _formatted(record)

    assert line["exc_type"] == "ValueError"
    assert "test_an_exception_writes_its_type" in str(line["stack"])
    assert "hunter2" not in json.dumps(line)


def _exc_info() -> tuple[type[BaseException], BaseException, TracebackType | None]:
    kind, error, frames = sys.exc_info()
    assert kind is not None
    assert error is not None
    return kind, error, frames


@pytest.mark.usefixtures("restored_logging")
def test_json_format_writes_app_and_uvicorn_lines_as_json() -> None:
    stream = io.StringIO()
    configure_logging(_settings(log_format="json", log_level="INFO"), stream=stream)

    logging.getLogger("ai_trainer.anything").info("from the app")
    logging.getLogger("uvicorn.error").info("Started server process")
    logging.getLogger("ai_trainer.anything").debug("below the level")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(line["logger"], line["message"]) for line in lines] == [
        ("ai_trainer.anything", "from the app"),
        ("uvicorn.error", "Started server process"),
    ]


@pytest.mark.usefixtures("restored_logging")
def test_text_format_writes_plain_lines_with_the_level() -> None:
    stream = io.StringIO()
    configure_logging(_settings(log_format="text", log_level="DEBUG"), stream=stream)

    logging.getLogger("ai_trainer.anything").debug("plain line")

    (line,) = stream.getvalue().splitlines()
    assert "DEBUG ai_trainer.anything plain line" in line
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


@pytest.mark.parametrize("log_format", ["json", "text"])
@pytest.mark.usefixtures("restored_logging")
def test_uvicorns_access_line_is_off_in_both_formats(log_format: str) -> None:
    """Uvicorn's access line carries the client address and the query string (ADR-0030
    invariant 3); it checks `hasHandlers()` before writing one."""
    stream = io.StringIO()
    configure_logging(_settings(log_format=log_format), stream=stream)

    access = logging.getLogger("uvicorn.access")
    access.info('%s - "%s %s HTTP/%s" %d', "203.0.113.9", "GET", "/?q=x", "1.1", 200)

    assert not access.hasHandlers()
    assert stream.getvalue() == ""
