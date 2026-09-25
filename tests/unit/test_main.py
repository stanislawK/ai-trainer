import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import Tracer
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import PostgresDsn, SecretStr
from pydantic_ai import Agent, InstrumentationSettings
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_trainer.main import _build_tracer_provider, _traces_endpoint, app_factory, create_app
from ai_trainer.settings import Settings

DATABASE_URL = "postgresql+psycopg://ai_trainer:secret@localhost:5432/ai_trainer"
OPENROUTER_API_KEY = "sk-or-v1-test"
EVAL_JUDGE_MODEL = "test/judge-model"
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


def test_create_app_refuses_an_unauthenticated_request_to_the_home_route() -> None:
    """No cookie means the gate (ticket #14) short-circuits before any database access, so
    this proves the wiring without needing a reachable Postgres."""
    app = create_app(_settings())
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 401


def test_create_app_serves_the_manifest_as_application_manifest_json() -> None:
    """AC1 (ticket #42): the PWA manifest is exempt from the auth gate (it lives under
    `/static/`) and is served with the manifest content type, not `StaticFiles`' generic
    fallback — `mimetypes.add_type` in `main.py` makes this hold regardless of the host OS's
    own `/etc/mime.types`."""
    app = create_app(_settings())
    client = TestClient(app)

    response = client.get("/static/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["theme_color"]


def test_create_app_serves_every_manifest_icon() -> None:
    """AC1 (ticket #42): every icon the manifest lists is actually reachable at 200, so a
    typo in a path never silently breaks installability."""
    app = create_app(_settings())
    client = TestClient(app)
    manifest = client.get("/static/manifest.webmanifest").json()

    assert len(manifest["icons"]) >= 2
    for icon in manifest["icons"]:
        response = client.get(icon["src"])
        assert response.status_code == 200, icon["src"]
        assert response.headers["content-type"] == icon["type"]


def test_create_app_serves_the_favicon_and_apple_touch_icon() -> None:
    """Ticket #42 requirements: an SVG favicon and a 180px apple-touch-icon, both linked from
    `layouts/base.html` and served alongside the manifest icons."""
    app = create_app(_settings())
    client = TestClient(app)

    favicon = client.get("/static/favicon.svg")
    apple_touch = client.get("/static/icons/apple-touch-icon.png")

    assert favicon.status_code == 200
    assert favicon.headers["content-type"] == "image/svg+xml"
    assert apple_touch.status_code == 200
    assert apple_touch.headers["content-type"] == "image/png"


def test_create_app_registers_the_admin_route() -> None:
    """Unlike the home-route check above, a 401 here wouldn't prove much: the gate (ticket
    #14) refuses an unauthenticated request to *any* non-exempt path, registered or not, so
    hitting `/admin` with no cookie can't tell "wired but gated" apart from "never wired"
    (ticket #40 skeptic finding). `app.openapi()` builds the schema in-process, the same way
    `scripts/generate_openapi.py` does, so it sees every registered route with no HTTP round
    trip to get gated."""
    app = create_app(_settings())

    assert "/admin" in app.openapi()["paths"]


def test_create_app_wires_the_designed_404_and_500_pages() -> None:
    """`register_error_handlers` (ticket #44) is called from `create_app`. A full HTTP round
    trip can't prove this here: `ActiveUserGateMiddleware` refuses any non-exempt, unmatched
    path with 401 for an unauthenticated request before routing ever runs (same reasoning as
    `test_create_app_registers_the_admin_route` above), and building a real `active` session
    needs a database this unit-level test doesn't have. `tests/integration/test_error_pages.py`
    proves the end-to-end behavior with a real signed-in session instead."""
    app = create_app(_settings())

    assert StarletteHTTPException in app.exception_handlers
    assert Exception in app.exception_handlers


def test_create_app_registers_the_settings_route() -> None:
    """Same reasoning as the admin-route check above (ticket #41): the gate refuses an
    unauthenticated request to any non-exempt path whether or not it's registered, so this
    checks the OpenAPI schema instead of an HTTP round trip."""
    app = create_app(_settings())

    assert "/settings" in app.openapi()["paths"]


def test_create_app_wires_settings_into_app() -> None:
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn(DATABASE_URL),
        openrouter_api_key=SecretStr(OPENROUTER_API_KEY),
        eval_judge_model=EVAL_JUDGE_MODEL,
    )

    app = create_app(settings)

    assert isinstance(app, FastAPI)
    assert app.state.settings is settings


async def test_create_app_disposes_its_engine_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh SQLAlchemy engine is opened per `create_app` call for the auth repositories;
    without disposing it on shutdown, a repeatedly-constructed app (a reload worker, or any
    test building the app more than once) would leak idle pooled connections."""
    disposed: list[AsyncEngine] = []
    original_dispose = AsyncEngine.dispose

    async def spy_dispose(self: AsyncEngine) -> None:
        disposed.append(self)
        await original_dispose(self)

    monkeypatch.setattr(AsyncEngine, "dispose", spy_dispose)

    app = create_app(_settings())
    with TestClient(app):
        assert disposed == []

    assert len(disposed) == 1


def test_app_factory_wires_settings_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)

    app = app_factory()

    assert isinstance(app, FastAPI)
    assert str(app.state.settings.database_url) == DATABASE_URL


def test_traces_endpoint_returns_none_when_unset() -> None:
    assert _traces_endpoint(None) is None


def test_traces_endpoint_appends_v1_traces_path() -> None:
    """Regression: `OTLPSpanExporter` skips its own auto-append when `endpoint=` is passed
    explicitly, so a bare Grafana Cloud host (as `.env.example` documents) must get the path
    appended here or spans get silently POSTed to the wrong URL (ADR-0018)."""
    assert (
        _traces_endpoint("https://otlp-gateway-prod-us-central-0.grafana.net/otlp")
        == "https://otlp-gateway-prod-us-central-0.grafana.net/otlp/v1/traces"
    )


def test_traces_endpoint_strips_trailing_slash_before_appending() -> None:
    assert _traces_endpoint("http://localhost:4318/") == "http://localhost:4318/v1/traces"


def test_traces_endpoint_does_not_double_append() -> None:
    already_correct = "https://collector.example.com/v1/traces"
    assert _traces_endpoint(already_correct) == already_correct


def test_build_tracer_provider_returns_none_when_otel_disabled() -> None:
    assert _build_tracer_provider(_settings(otel_enabled=False)) is None


def test_build_tracer_provider_names_the_configured_service() -> None:
    provider = _build_tracer_provider(
        _settings(
            otel_enabled=True,
            otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT,
            otel_service_name="ai-trainer-test",
        )
    )

    assert provider is not None
    assert provider.resource.attributes["service.name"] == "ai-trainer-test"


def test_create_app_does_not_instrument_agents_when_otel_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(Agent, "instrument_all", staticmethod(calls.append))

    create_app(_settings(otel_enabled=False))

    assert calls == []


def test_create_app_instruments_agents_with_no_content_when_otel_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[InstrumentationSettings] = []
    monkeypatch.setattr(Agent, "instrument_all", staticmethod(calls.append))

    create_app(_settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT))

    assert len(calls) == 1
    assert calls[0].include_content is False
    # A real SDK `Tracer` (vs. `ProxyTracer`) proves a concrete `TracerProvider` was wired in,
    # not the no-op global default (ADR-0018 invariant 3).
    assert isinstance(calls[0].tracer, Tracer)


async def test_instrumented_agent_run_names_the_model_and_excludes_content() -> None:
    """An agent run through `_build_tracer_provider`'s real wiring (only the exporter transport
    swapped for an in-memory one) produces a content-free span naming the model (AC1, AC2)."""
    exporter = InMemorySpanExporter()
    provider = _build_tracer_provider(
        _settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT),
        span_exporter=exporter,
    )
    assert provider is not None
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider, include_content=False))

    secret_prompt = "a very private training question"
    await Agent(TestModel()).run(secret_prompt)
    provider.force_flush(timeout_millis=2000)

    spans = exporter.get_finished_spans()
    assert spans, "expected at least one span from the instrumented agent run"
    assert any(span.attributes and span.attributes.get("gen_ai.request.model") for span in spans)
    for span in spans:
        serialized = repr(dict(span.attributes or {}))
        assert secret_prompt not in serialized
