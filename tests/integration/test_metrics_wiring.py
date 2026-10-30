"""The model-call budget's refusals reach the metrics through the real composition root
(ADR-0029, ADR-0030, #275). OpenRouter refuses every request below the instrumented
transport, as in the tracing tests, so no socket opens."""

import re

from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai_trainer.domain.users import User
from ai_trainer.main import create_app
from ai_trainer.settings import Settings
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME
from tests.integration.test_request_tracing import (
    _CSRF_TOKEN_IN_HTML,
    _HTMX,
    UNREACHABLE_ENDPOINT,
    athlete,
    openrouter_refuses,
)

__all__ = ["athlete", "openrouter_refuses"]


def test_a_reply_past_the_hourly_budget_counts_its_refused_model_calls(
    athlete: tuple[User, str], openrouter_refuses: list[str]
) -> None:
    """One call an hour: the reply's first call is sent, every other is refused and counted."""
    user, session_id = athlete
    reader = InMemoryMetricReader()
    settings = Settings(
        otel_enabled=True,
        otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT,
        llm_calls_per_user_per_hour=1,
    )
    client = TestClient(
        create_app(settings, metric_reader=reader, span_exporter=InMemorySpanExporter())
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    token = _CSRF_TOKEN_IN_HTML.search(client.get("/").text)
    assert token is not None
    sent = client.post(
        "/messages",
        data={"message": "hi!"},
        headers={CSRF_HEADER_NAME: token.group(1), **_HTMX},
    )
    message_id = re.search(r"/messages/([0-9a-f-]{36})/reply", sent.text)
    assert message_id is not None, sent.text

    assert client.get(f"/messages/{message_id.group(1)}/reply").status_code == 200

    data = reader.get_metrics_data()
    assert data is not None
    refusals = [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "ai_trainer.rate_limit.refusals"
        for point in metric.data.data_points
    ]
    assert [dict(point.attributes or {}) for point in refusals] == [
        {"limit_kind": "model_calls_per_hour"}
    ]
    assert isinstance(refusals[0], NumberDataPoint)
    assert refusals[0].value >= 1
    # Every point the real wiring made, the instrumentors' included if they made any (#275 AC4).
    labels = [
        str(value)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        for point in metric.data.data_points
        for value in (point.attributes or {}).values()
    ]
    assert {
        scope.scope.name for resource in data.resource_metrics for scope in resource.scope_metrics
    } == {"ai_trainer"}
    for value in labels:
        assert str(user.id) not in value
        assert message_id.group(1) not in value
        assert sent.headers["X-Request-ID"] not in value
