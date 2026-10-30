"""App and model-call metrics (ADR-0030, #275): what each instrument records, under which
bounded labels, and that recording or exporting never fails the caller."""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.metrics import NoOpMeter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
    MetricsData,
    NumberDataPoint,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import PostgresDsn, SecretStr

from ai_trainer.adapters.metrics import OtelMetrics, status_class
from ai_trainer.application.metered_calls import MeteredLlmCallsRepository
from ai_trainer.application.model_budget import ModelCallBudget
from ai_trainer.application.ports.metrics import NO_METRICS, LimitKind
from ai_trainer.domain.llm_calls import LlmCall, LlmCallOutcome, NewLlmCall
from ai_trainer.main import _build_meter_provider, _build_resource, _metrics_endpoint, create_app
from ai_trainer.settings import Settings
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.rate_limit import RateLimitMiddleware
from ai_trainer.web.request_log import log_every_request
from ai_trainer.web.templating import build_templates

_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
_USER = uuid4()


class FixedClock:
    def now(self) -> datetime:
        return _NOW


class RecordingMetrics:
    """Implements `MetricsPort`, keeping what it was told."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, int]] = []
        self.calls: list[NewLlmCall] = []
        self.refusals: list[LimitKind] = []
        self.log_turns: list[float] = []

    def request(self, *, method: str, route: str, status: int, seconds: float) -> None:
        self.requests.append((method, route, status))

    def model_call(self, call: NewLlmCall) -> None:
        self.calls.append(call)

    def refusal(self, kind: LimitKind) -> None:
        self.refusals.append(kind)

    def log_turn(self, seconds: float) -> None:
        self.log_turns.append(seconds)


def _call(outcome: LlmCallOutcome = LlmCallOutcome.SUCCESS) -> NewLlmCall:
    return NewLlmCall(
        user_id=_USER,
        template_id="router",
        template_version=3,
        model="z-ai/glm-5.3-flash",
        input_tokens=120,
        output_tokens=30,
        cost=Decimal("0.00042"),
        latency_ms=1250,
        outcome=outcome,
    )


def _labels(point: Any) -> dict[str, object]:
    return dict(point.attributes or {})


def _metrics() -> tuple[OtelMetrics, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return OtelMetrics(provider.get_meter("ai_trainer")), reader


def _points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    assert data is not None
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def test_a_request_is_one_duration_point_by_method_route_and_status_class() -> None:
    metrics, reader = _metrics()

    metrics.request(method="POST", route="/messages", status=201, seconds=0.05)

    [point] = _points(reader, "http.server.request.duration")
    assert isinstance(point, HistogramDataPoint)
    assert point.count == 1
    assert point.sum == pytest.approx(0.05)
    assert _labels(point) == {
        "http.request.method": "POST",
        "http.route": "/messages",
        "http.response.status_class": "2xx",
    }


@pytest.mark.parametrize(
    ("status", "expected"), [(101, "1xx"), (200, "2xx"), (303, "3xx"), (429, "4xx"), (503, "5xx")]
)
def test_status_class_is_the_hundreds(status: int, expected: str) -> None:
    assert status_class(status) == expected


def test_a_model_call_records_its_rows_latency_and_cost_by_template_model_and_outcome() -> None:
    metrics, reader = _metrics()

    metrics.model_call(_call(LlmCallOutcome.TIMEOUT))

    data = reader.get_metrics_data()
    assert data is not None
    by_name = {
        metric.name: list(metric.data.data_points)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    [duration] = by_name["ai_trainer.llm.call.duration"]
    [cost] = by_name["ai_trainer.llm.call.cost"]
    assert isinstance(duration, HistogramDataPoint)
    assert isinstance(cost, NumberDataPoint)
    labels = {
        "template_id": "router",
        "template_version": 3,
        "model": "z-ai/glm-5.3-flash",
        "outcome": "timeout",
    }
    assert (duration.count, duration.sum, _labels(duration)) == (1, 1.25, labels)
    assert cost.value == pytest.approx(0.00042)
    assert _labels(cost) == labels


def test_a_call_with_no_cost_adds_nothing_to_the_cost_but_is_still_counted() -> None:
    metrics, reader = _metrics()

    metrics.model_call(_call().model_copy(update={"cost": None}))

    data = reader.get_metrics_data()
    assert data is not None
    names = [
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    ]
    assert names == ["ai_trainer.llm.call.duration"]


def test_a_refusal_is_counted_by_its_limit_kind() -> None:
    metrics, reader = _metrics()

    metrics.refusal("session")
    metrics.refusal("session")
    metrics.refusal("model_calls_in_flight")

    points = _points(reader, "ai_trainer.rate_limit.refusals")
    assert sorted((_labels(p)["limit_kind"], p.value) for p in points) == [
        ("model_calls_in_flight", 1),
        ("session", 2),
    ]


def test_a_log_turn_lands_in_the_g14_buckets() -> None:
    metrics, reader = _metrics()

    metrics.log_turn(4.2)

    [point] = _points(reader, "ai_trainer.log_turn.duration")
    assert isinstance(point, HistogramDataPoint)
    assert point.count == 1
    assert 5.0 in point.explicit_bounds
    assert 10.0 in point.explicit_bounds
    assert _labels(point) == {}


class _BrokenInstrument:
    def record(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("instrument broke")

    add = record


class _BrokenMeter(NoOpMeter):
    def create_histogram(self, *args: Any, **kwargs: Any) -> Any:
        return _BrokenInstrument()

    def create_counter(self, *args: Any, **kwargs: Any) -> Any:
        return _BrokenInstrument()


def test_a_failing_instrument_never_raises_into_the_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = OtelMetrics(_BrokenMeter("broken"))

    metrics.request(method="GET", route="/", status=200, seconds=0.01)
    metrics.model_call(_call())
    metrics.refusal("sign_in")
    metrics.log_turn(1.0)

    assert "metric not recorded" in caplog.text


class _FailingExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.exports = 0

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any
    ) -> MetricExportResult:
        self.exports += 1
        raise ConnectionError("collector unreachable")

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        pass


def test_a_failing_export_never_raises_into_the_caller() -> None:
    exporter = _FailingExporter()
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=60_000)
    provider = MeterProvider(metric_readers=[reader])
    metrics = OtelMetrics(provider.get_meter("ai_trainer"))

    metrics.model_call(_call())
    provider.force_flush(timeout_millis=2000)
    provider.shutdown()

    assert exporter.exports >= 1


class FakeCalls:
    """Implements `LlmCallsRepositoryPort` in memory."""

    def __init__(self) -> None:
        self.rows: list[NewLlmCall] = []

    async def record(self, call: NewLlmCall) -> LlmCall:
        self.rows.append(call)
        return LlmCall(**call.model_dump(), id=uuid4(), created_at=_NOW)

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        return []


async def test_a_metered_row_records_the_metric_from_the_same_values() -> None:
    inner = FakeCalls()
    metrics = RecordingMetrics()
    calls = MeteredLlmCallsRepository(inner, metrics)

    row = await calls.record(_call(LlmCallOutcome.ERROR))

    assert metrics.calls == inner.rows == [_call(LlmCallOutcome.ERROR)]
    assert row.outcome is LlmCallOutcome.ERROR
    assert await calls.list_for_user(_USER) == []


async def test_a_metric_that_fails_still_writes_the_row() -> None:
    inner = FakeCalls()
    calls = MeteredLlmCallsRepository(inner, OtelMetrics(_BrokenMeter("broken")))

    await calls.record(_call())

    assert inner.rows == [_call()]


def test_the_budget_names_which_of_its_limits_refused() -> None:
    metrics = RecordingMetrics()
    in_flight = ModelCallBudget(FixedClock(), per_hour=10, in_flight=1, metrics=metrics)
    hourly = ModelCallBudget(FixedClock(), per_hour=1, in_flight=5, metrics=metrics)

    assert in_flight.try_acquire(_USER)
    assert not in_flight.try_acquire(_USER)
    assert hourly.try_acquire(_USER)
    hourly.release(_USER)
    assert not hourly.try_acquire(_USER)

    assert metrics.refusals == ["model_calls_in_flight", "model_calls_per_hour"]


def test_the_budget_records_nothing_by_default() -> None:
    budget = ModelCallBudget(FixedClock(), per_hour=1, in_flight=1)

    assert budget.try_acquire(_USER)
    assert not budget.try_acquire(_USER)


def _limited_app(metrics: RecordingMetrics) -> FastAPI:
    app = FastAPI()

    @app.get("/{full_path:path}")
    async def anything(full_path: str) -> dict[str, str]:
        return {"path": full_path}

    app.add_middleware(
        RateLimitMiddleware,
        clock=FixedClock(),
        templates=build_templates(),
        auth_per_minute=1,
        session_per_minute=1,
        metrics=metrics,
    )
    return app


def test_a_refused_request_records_its_limit_kind() -> None:
    metrics = RecordingMetrics()
    client = TestClient(_limited_app(metrics))

    assert client.get("/sign-in").status_code == 200
    assert client.get("/sign-in").status_code == 429
    client.cookies.set(SESSION_COOKIE_NAME, "session-a")
    assert client.get("/messages").status_code == 200
    assert client.get("/messages").status_code == 429

    assert metrics.refusals == ["sign_in", "session"]


def _logged_app(metrics: RecordingMetrics) -> FastAPI:
    app = FastAPI()

    @app.get("/c/{conversation_id}")
    async def conversation(conversation_id: str) -> dict[str, str]:
        return {"id": conversation_id}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    log_every_request(app, metrics)
    return app


def test_each_request_is_recorded_once_under_its_route_template_and_health_is_not() -> None:
    metrics = RecordingMetrics()
    client = TestClient(_logged_app(metrics), raise_server_exceptions=False)
    conversation_id = str(uuid4())

    client.get(f"/c/{conversation_id}")
    client.get("/health")
    client.get("/boom")
    client.get(f"/no/such/{conversation_id}")
    client.request("BREW", "/boom")

    assert metrics.requests == [
        ("GET", "/c/{conversation_id}", 200),
        ("GET", "/boom", 500),
        ("GET", "unmatched", 404),
        ("_OTHER", "/boom", 405),
    ]


def test_log_every_request_records_nothing_without_metrics() -> None:
    app = FastAPI()

    @app.get("/")
    async def index() -> dict[str, str]:
        return {}

    log_every_request(app)

    assert TestClient(app).get("/").status_code == 200


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        "openrouter_api_key": SecretStr("sk-or-v1-test"),
        "eval_judge_model": "test/judge-model",
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


def test_no_meter_provider_is_built_when_otel_is_off() -> None:
    settings = _settings()
    assert _build_meter_provider(settings, _build_resource(settings)) is None


def test_the_meter_provider_exports_over_otlp_with_the_traces_resource() -> None:
    settings = _settings(otel_enabled=True, otel_exporter_otlp_endpoint="http://127.0.0.1:1")
    provider = _build_meter_provider(settings, _build_resource(settings))
    assert provider is not None
    provider.shutdown(timeout_millis=100)


def test_metrics_endpoint_appends_v1_metrics_once() -> None:
    assert _metrics_endpoint(None) is None
    assert _metrics_endpoint("http://alloy:4318/") == "http://alloy:4318/v1/metrics"
    assert _metrics_endpoint("http://alloy:4318/v1/metrics") == "http://alloy:4318/v1/metrics"


def _names(reader: InMemoryMetricReader) -> Sequence[str]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    ]


def test_create_app_records_request_metrics_with_the_service_resource() -> None:
    reader = InMemoryMetricReader()
    app = create_app(
        _settings(otel_enabled=True, otel_exporter_otlp_endpoint="http://127.0.0.1:1"),
        metric_reader=reader,
        span_exporter=InMemorySpanExporter(),
    )

    TestClient(app).get("/sign-in")

    data = reader.get_metrics_data()
    assert data is not None
    [resource] = data.resource_metrics
    assert resource.resource.attributes["service.name"] == "ai-trainer"
    assert [m.name for s in resource.scope_metrics for m in s.metrics] == [
        "http.server.request.duration"
    ]


def test_create_app_records_no_metrics_when_otel_is_off() -> None:
    reader = InMemoryMetricReader()
    app = create_app(_settings(), metric_reader=reader)

    TestClient(app).get("/sign-in")

    assert _names(reader) == []


def test_no_metrics_accepts_every_recording() -> None:
    NO_METRICS.request(method="GET", route="/", status=200, seconds=0.1)
    NO_METRICS.model_call(_call())
    NO_METRICS.refusal("session")
    NO_METRICS.log_turn(1.0)


def test_create_app_counts_a_refused_sign_in_by_its_limit_kind() -> None:
    """#275 AC3 through the real wiring."""
    reader = InMemoryMetricReader()
    app = create_app(
        _settings(
            otel_enabled=True,
            otel_exporter_otlp_endpoint="http://127.0.0.1:1",
            auth_requests_per_minute=1,
        ),
        metric_reader=reader,
        span_exporter=InMemorySpanExporter(),
    )
    client = TestClient(app)

    assert client.get("/sign-in").status_code == 200
    assert client.get("/sign-in").status_code == 429

    [refusal] = _points(reader, "ai_trainer.rate_limit.refusals")
    assert (_labels(refusal), refusal.value) == ({"limit_kind": "sign_in"}, 1)
