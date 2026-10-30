"""Real-socket telemetry behaviour (ADR-0018) — belongs in integration, not unit (ADR-0013)."""

import time
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from pydantic import PostgresDsn, SecretStr
from pydantic_ai import Agent, InstrumentationSettings
from pydantic_ai.models.test import TestModel

from ai_trainer import main
from ai_trainer.main import (
    _OTLP_EXPORT_TIMEOUT_SECONDS,
    _build_meter_provider,
    _build_resource,
    create_app,
)
from ai_trainer.settings import Settings

DATABASE_URL = "postgresql+psycopg://ai_trainer:secret@localhost:5432/ai_trainer"
OPENROUTER_API_KEY = "sk-or-v1-test"
EVAL_JUDGE_MODEL = "test/judge-model"
# Guaranteed-closed local port (no service ever listens on port 1): connections fail fast with
# ECONNREFUSED, so this never reaches out over the real network.
UNREACHABLE_ENDPOINT = "http://127.0.0.1:1"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": PostgresDsn(DATABASE_URL),
        "openrouter_api_key": SecretStr(OPENROUTER_API_KEY),
        "eval_judge_model": EVAL_JUDGE_MODEL,
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _providers_shut_down(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """These tests keep the real OTLP exporters, so each provider they build is shut down when
    the test ends; left alone, its exporter retried the closed port at interpreter exit (#328)."""
    built: list[TracerProvider | MeterProvider] = []

    def kept[**P, T: TracerProvider | MeterProvider | None](
        build: Callable[P, T],
    ) -> Callable[P, T]:
        def _build(*args: P.args, **kwargs: P.kwargs) -> T:
            provider = build(*args, **kwargs)
            if provider is not None:
                built.append(provider)
            return provider

        return _build

    monkeypatch.setattr(main, "_build_tracer_provider", kept(main._build_tracer_provider))
    monkeypatch.setattr(main, "_build_meter_provider", kept(main._build_meter_provider))
    yield
    for provider in built:
        provider.shutdown()


async def test_unreachable_otlp_endpoint_does_not_raise_into_the_caller() -> None:
    """AC4: flag on, endpoint unreachable — the agent run still succeeds and a later flush
    of the buffered span (a stand-in for the request's telemetry work) never raises."""
    provider = main._build_tracer_provider(
        _settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT)
    )
    assert provider is not None
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider, include_content=False))

    result = await Agent(TestModel()).run("hello")

    assert result.output is not None
    provider.force_flush(timeout_millis=6000)


def test_an_unreachable_otlp_endpoint_neither_fails_nor_slows_a_traced_request() -> None:
    """#271 AC5: request spans queue for the background exporter, so requests answer as
    fast as untraced ones while every export to the closed port fails."""
    client = TestClient(
        create_app(_settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT))
    )

    started = time.perf_counter()
    statuses = [client.get("/", follow_redirects=False).status_code for _ in range(5)]
    elapsed = time.perf_counter() - started

    assert statuses == [303] * 5
    assert elapsed < _OTLP_EXPORT_TIMEOUT_SECONDS / 2
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider.force_flush(timeout_millis=6000)


def test_an_unreachable_otlp_endpoint_neither_fails_nor_slows_a_metered_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#275 AC5 with the real OTLP metric exporter: it exports every 50 ms to the closed port
    while the requests run, and each export fails on its own thread."""
    monkeypatch.setattr(main, "_METRIC_EXPORT_INTERVAL_MS", 50)
    client = TestClient(
        create_app(_settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT))
    )

    started = time.perf_counter()
    statuses = []
    for _ in range(5):
        statuses.append(client.get("/", follow_redirects=False).status_code)
        time.sleep(0.06)
    elapsed = time.perf_counter() - started

    assert statuses == [303] * 5
    assert elapsed < _OTLP_EXPORT_TIMEOUT_SECONDS / 2


def test_a_failed_metric_export_is_bounded_and_never_raises() -> None:
    settings = _settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT)
    provider = _build_meter_provider(settings, _build_resource(settings))
    assert provider is not None
    provider.get_meter("ai_trainer").create_counter("probe").add(1)

    started = time.perf_counter()
    provider.force_flush(timeout_millis=10_000)
    provider.shutdown(timeout_millis=10_000)

    assert time.perf_counter() - started < 2 * _OTLP_EXPORT_TIMEOUT_SECONDS + 1
