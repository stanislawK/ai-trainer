"""Composition root: the only place concrete classes are wired (ADR-0003)."""

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.util.re import parse_env_headers
from pydantic_ai import Agent, InstrumentationSettings
from starlette.middleware.sessions import SessionMiddleware

from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.google_oauth import AuthlibGoogleOAuthClient
from ai_trainer.adapters.health import PsycopgDatabaseHealth
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.settings import Settings
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.auth import build_auth_router
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.errors import register_error_handlers
from ai_trainer.web.health import build_health_router
from ai_trainer.web.home import build_home_router
from ai_trainer.web.settings import build_settings_router
from ai_trainer.web.sign_in import build_sign_in_router
from ai_trainer.web.templating import STATIC_DIR, build_templates

# Bounds a single export attempt (including its retries) so a request that later flushes or
# shuts down the provider is never stalled for long by an unreachable collector (ADR-0018).
_OTLP_EXPORT_TIMEOUT_SECONDS = 5.0

_GOOGLE_SERVER_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"

# The stdlib's mimetypes registry only maps `.webmanifest` on systems whose `/etc/mime.types`
# happens to list it; registering it here makes `StaticFiles`' content-type for the PWA
# manifest correct regardless of the host OS (ticket #42, PRD-0003 G11).
mimetypes.add_type("application/manifest+json", ".webmanifest")


def create_app(settings: Settings) -> FastAPI:
    engine = build_engine(str(settings.database_url))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        # Closes the SQLAlchemy pool's connections on shutdown; a fresh engine is otherwise
        # opened by every `create_app` call, so a repeatedly-constructed app (a reload worker,
        # or a test that never uses the app as a context manager) would leak idle connections.
        await engine.dispose()

    app = FastAPI(title="ai-trainer", lifespan=lifespan)
    app.state.settings = settings
    # Signs Authlib's transient OAuth-state cookie only; distinct from the app's own
    # PostgreSQL-backed session cookie set by the auth router (ADR-0005).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret_key.get_secret_value(),
        https_only=settings.session_cookie_secure,
    )

    session_factory = build_session_factory(engine)
    users = SqlAlchemyUsersRepository(session_factory)
    sessions = SqlAlchemySessionsRepository(session_factory)
    status_changer = SqlAlchemyUserStatusChanger(session_factory)
    clock = UtcClock()
    templates = build_templates()
    register_error_handlers(app, templates)

    # Gates every route but the exemptions it names for itself (sign-in, callback, sign-out,
    # health, static) on an `active` user, resolved fresh on every request (ADR-0005
    # invariants 2 and 7, ticket #14).
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=clock,
        templates=templates,
        admin_emails=settings.admin_emails,
    )
    # Outermost among the two: runs before the gate above, so `request.state.csrf_token` is
    # already set when the gate renders the unauthorized/status pages (ADR-0005 invariant 3,
    # ticket #15).
    app.add_middleware(
        CsrfMiddleware,
        secret_key=settings.csrf_secret_key.get_secret_value().encode(),
        templates=templates,
    )

    health_port = PsycopgDatabaseHealth(str(settings.database_url))
    app.include_router(build_health_router(health_port))
    app.include_router(build_home_router(templates))
    app.include_router(
        build_admin_router(
            templates=templates, users=users, sessions=sessions, status_changer=status_changer
        )
    )
    app.include_router(build_settings_router(templates, users=users))
    app.include_router(
        build_sign_in_router(templates=templates, users=users, sessions=sessions, clock=clock)
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    oauth = OAuth()
    oauth.register(
        "google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        server_metadata_url=_GOOGLE_SERVER_METADATA_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    app.include_router(
        build_auth_router(
            oauth_client=AuthlibGoogleOAuthClient(oauth.google),
            users=users,
            sessions=sessions,
            clock=clock,
            admin_emails=settings.admin_emails,
            session_ttl=timedelta(days=settings.session_ttl_days),
            cookie_secure=settings.session_cookie_secure,
        )
    )

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
