"""Composition root: the only place concrete classes are wired (ADR-0003)."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.util.re import parse_env_headers
from pydantic_ai import Agent, InstrumentationSettings

from ai_trainer.adapters.health import PsycopgDatabaseHealth
from ai_trainer.settings import Settings
from ai_trainer.web.health import build_health_router
from ai_trainer.web.home import build_home_router
from ai_trainer.web.templating import STATIC_DIR, build_templates

# Bounds a single export attempt (including its retries) so a request that later flushes or
# shuts down the provider is never stalled for long by an unreachable collector (ADR-0018).
_OTLP_EXPORT_TIMEOUT_SECONDS = 5.0


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="ai-trainer")
    app.state.settings = settings
    health_port = PsycopgDatabaseHealth(str(settings.database_url))
    app.include_router(build_health_router(health_port))
    app.include_router(build_home_router(build_templates()))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    tracer_provider = _build_tracer_provider(settings)
    if tracer_provider is not None:
        Agent.instrument_all(
            InstrumentationSettings(tracer_provider=tracer_provider, include_content=False)
        )

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn ai_trainer.main:app_factory --factory`."""
    return create_app(Settings())


def _build_tracer_provider(
    settings: Settings, span_exporter: SpanExporter | None = None
) -> TracerProvider | None:
    """Builds the OTLP tracer provider from `Settings`, off by default (ADR-0018).

    An unreachable or failing OTLP endpoint never raises here or later: `BatchSpanProcessor`
    exports on a background thread and swallows export failures, so a request that triggers
    an instrumented agent run always completes regardless of the collector's reachability.

    `span_exporter` lets tests substitute an in-memory exporter to observe what this exact
    wiring produces; `create_app` never passes it, so production always gets the real OTLP
    HTTP exporter built from `settings`.
    """
    if not settings.otel_enabled:
        return None
    resource = Resource.create({"service.name": settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    exporter = span_exporter if span_exporter is not None else _build_otlp_span_exporter(settings)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


_TRACES_EXPORT_PATH = "v1/traces"


def _build_otlp_span_exporter(settings: Settings) -> OTLPSpanExporter:
    return OTLPSpanExporter(
        endpoint=_traces_endpoint(settings.otel_exporter_otlp_endpoint),
        headers=dict(parse_env_headers(settings.otel_exporter_otlp_headers or "", liberal=True)),
        timeout=_OTLP_EXPORT_TIMEOUT_SECONDS,
    )


def _traces_endpoint(base_endpoint: str | None) -> str | None:
    """Appends the OTLP HTTP traces path to a configured base endpoint.

    `OTLPSpanExporter` only appends this itself when it falls back to reading
    `OTEL_EXPORTER_OTLP_ENDPOINT` from the environment directly; passing `endpoint=`
    explicitly — required to source it from `Settings` rather than read the environment
    outside it — bypasses that. Without this, a bare Grafana Cloud host as documented in
    `.env.example` would get spans POSTed to the wrong path, failing silently since
    `BatchSpanProcessor` swallows export errors (ADR-0018).
    """
    if not base_endpoint:
        return None
    if base_endpoint.rstrip("/").endswith(_TRACES_EXPORT_PATH):
        return base_endpoint
    return f"{base_endpoint.rstrip('/')}/{_TRACES_EXPORT_PATH}"
