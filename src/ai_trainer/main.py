"""Composition root: the only place concrete classes are wired (ADR-0003)."""

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.util.re import parse_env_headers
from pydantic_ai import Agent, InstrumentationSettings
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.clarification_repository import SqlAlchemyClarificationRepository
from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.google_oauth import AuthlibGoogleOAuthClient
from ai_trainer.adapters.health import PsycopgDatabaseHealth
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.log_records import configure_logging
from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.telemetry import ScrubbingSpanExporter
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.application.budgeted_gateways import BudgetedDecisionGateway, BudgetedLlmGateway
from ai_trainer.application.model_budget import ModelCallBudget
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.llm_gateway import LlmGatewayPort
from ai_trainer.application.reply import ReplyServices
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.llm.conversation import LlmConversation, build_prompt_registry
from ai_trainer.llm.debug_log import build_debug_log
from ai_trainer.llm.decision_gateway import OpenRouterDecisionGateway
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.llm.onboarding import LlmOnboardingReader
from ai_trainer.llm.router_fast import JevFastRouter, RouterFastThresholds
from ai_trainer.llm.safety_screen import JevSafetyScreen
from ai_trainer.settings import Settings
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.ai_onboarding import build_ai_onboarding_router
from ai_trainer.web.auth import build_auth_router
from ai_trainer.web.chat import build_chat_router
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.errors import register_error_handlers
from ai_trainer.web.health import build_health_router
from ai_trainer.web.rate_limit import RateLimitMiddleware
from ai_trainer.web.request_log import log_every_request
from ai_trainer.web.settings import build_settings_router
from ai_trainer.web.sign_in import build_sign_in_router
from ai_trainer.web.templating import STATIC_DIR, build_templates

_PROMPTS_ROOT = Path(__file__).resolve().parent / "llm" / "prompts"

# Bounds a single export attempt (including its retries) so a request that later flushes or
# shuts down the provider is never stalled for long by an unreachable collector (ADR-0018).
_OTLP_EXPORT_TIMEOUT_SECONDS = 5.0

# Regexes searched in each request's URL without its query (ADR-0030).
_UNTRACED_URLS = r"^https?://[^/]+/health/?$"

_GOOGLE_SERVER_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"

# The stdlib's mimetypes registry only maps `.webmanifest` on systems whose `/etc/mime.types`
# happens to list it; registering it here makes `StaticFiles`' content-type for the PWA
# manifest correct regardless of the host OS (ticket #42, PRD-0003 G11).
mimetypes.add_type("application/manifest+json", ".webmanifest")


def build_llm_conversation(
    settings: Settings, sports: SportRegistry, gateway: LlmGatewayPort
) -> LlmConversation:
    """Each template's model, provider preferences, reasoning effort and call timeout from
    `Settings` (ADR-0026)."""
    return LlmConversation(
        registry=build_prompt_registry(_PROMPTS_ROOT, sports),
        gateway=gateway,
        sports=sports,
        router_model=settings.router_model,
        router_provider=settings.router_provider_preferences,
        router_timeout_seconds=settings.router_call_timeout_seconds,
        router_reasoning_effort=settings.router_reasoning_effort,
        chitchat_model=settings.chitchat_model,
        extract_model=settings.extract_model,
        extract_provider=settings.extract_provider_preferences,
        extract_timeout_seconds=settings.extract_call_timeout_seconds,
        extract_reasoning_effort=settings.extract_reasoning_effort,
        history_turns=settings.chat_history_turns,
    )


def build_onboarding_reader(
    settings: Settings, sports: SportRegistry, gateway: LlmGatewayPort, clock: ClockPort
) -> LlmOnboardingReader:
    """The onboarding reads' one settings group (ADR-0028)."""
    return LlmOnboardingReader(
        registry=build_prompt_registry(_PROMPTS_ROOT, sports),
        gateway=gateway,
        clock=clock,
        model=settings.onboarding_model,
        provider=settings.onboarding_provider_preferences,
        reasoning_effort=settings.onboarding_reasoning_effort,
        timeout_seconds=settings.onboarding_call_timeout_seconds,
    )


def create_app(settings: Settings, *, span_exporter: SpanExporter | None = None) -> FastAPI:
    """`span_exporter` lets tests keep spans in memory; production always exports over OTLP."""
    engine = build_engine(str(settings.database_url))

    # The onboarding's background reads, held so none is collected mid-read (ADR-0028).
    reads = BackgroundReads()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        # A read cut off here stays `pending`; the summary reads it again (ADR-0028).
        await reads.close()
        # Closes the SQLAlchemy pool's connections on shutdown; a fresh engine is otherwise
        # opened by every `create_app` call, so a repeatedly-constructed app (a reload worker,
        # or a test that never uses the app as a context manager) would leak idle connections.
        await engine.dispose()

    app = FastAPI(title="ai-trainer", lifespan=lifespan)
    app.state.settings = settings
    app.state.sport_registry = default_sport_registry()
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
    onboarding = SqlAlchemyOnboardingRepository(session_factory)
    chat = SqlAlchemyChatRepository(session_factory)
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
    sport_registry = app.state.sport_registry
    llm_calls = SqlAlchemyLlmCallsRepository(session_factory)
    debug_log = build_debug_log(settings, clock)
    # One budget for text calls and decision calls (ADR-0029). Evals build their own gateways.
    budget = ModelCallBudget(
        clock,
        per_hour=settings.llm_calls_per_user_per_hour,
        in_flight=settings.llm_calls_in_flight_per_user,
    )
    gateway = BudgetedLlmGateway(
        OpenRouterGateway(settings, llm_calls, debug_log=debug_log), budget
    )
    conversation = build_llm_conversation(settings, sport_registry, gateway)
    decisions = BudgetedDecisionGateway(
        OpenRouterDecisionGateway(settings, llm_calls, debug_log=debug_log), budget
    )
    replies = ReplyServices(
        chat=chat,
        conversations=SqlAlchemyConversationRepository(
            session_factory, clock=clock, retention_days=settings.conversation_retention_days
        ),
        drafts=SqlAlchemySessionDraftRepository(session_factory, sport_registry),
        clarifications=SqlAlchemyClarificationRepository(session_factory),
        sessions=SqlAlchemyTrainingSessionRepository(session_factory),
        sports=sport_registry,
        profile=onboarding,
        conversation=conversation,
        # Routes a message with one confident intent before the LLM router (ADR-0020).
        fast_router=JevFastRouter(
            decisions,
            thresholds=RouterFastThresholds(
                multi_request=settings.router_fast_multi_request_threshold,
                intent=settings.router_fast_intent_threshold,
                sport=settings.router_fast_sport_threshold,
                plausible_floor=settings.router_fast_plausible_sport_floor,
            ),
        ),
        # Runs alongside the router and can only add the safety intent (ADR-0020, G3).
        safety_screen=JevSafetyScreen(
            decisions,
            threshold=settings.safety_screen_pain_threshold,
        ),
        clock=clock,
        history_turns=settings.chat_history_turns,
        merge_window=timedelta(minutes=settings.session_merge_window_min),
        max_clarification_options=settings.max_clarification_options,
        recent_sessions=settings.extract_recent_sessions,
    )
    app.include_router(
        build_chat_router(
            templates,
            chat=chat,
            replies=replies,
            sports=app.state.sport_registry,
            retention_days=settings.conversation_retention_days,
        )
    )
    app.include_router(
        build_admin_router(
            templates=templates, users=users, sessions=sessions, status_changer=status_changer
        )
    )
    app.include_router(build_settings_router(templates, users=users))
    app.include_router(
        build_ai_onboarding_router(
            templates,
            registry=sport_registry,
            answers=onboarding,
            reads=reads,
            reader=build_onboarding_reader(settings, sport_registry, gateway, clock),
            clock=clock,
        )
    )
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
            status_changer=status_changer,
            clock=clock,
            admin_emails=settings.admin_emails,
            session_ttl=timedelta(days=settings.session_ttl_days),
            cookie_secure=settings.session_cookie_secure,
            public_base_url=settings.public_base_url,
        )
    )

    # Inside the host check, outside CSRF and the sign-in gate (ADR-0029). Added before
    # TrustedHostMiddleware, which is added last and so stays the outermost.
    app.add_middleware(
        RateLimitMiddleware,
        clock=clock,
        templates=templates,
        auth_per_minute=settings.auth_requests_per_minute,
        session_per_minute=settings.session_requests_per_minute,
    )
    # A request whose Host is not the public origin's is refused with 400 before the session,
    # CSRF or sign-in gate sees it (ADR-0023). The port is not compared, so the host alone is
    # allowed.
    public_host = urlsplit(settings.public_base_url).hostname
    assert public_host is not None  # `Settings` only accepts an origin with a host
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[public_host])
    # Around Starlette's error middleware too, so a refused or failed request also gets a
    # `request_id` and an access line (ADR-0030).
    log_every_request(app)

    tracer_provider = _build_tracer_provider(settings, span_exporter)
    if tracer_provider is not None:
        _trace_every_request(app, engine, tracer_provider)

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn ai_trainer.main:app_factory --factory`.

    Logging is configured here, not in `create_app`, so tests building apps keep pytest's.
    """
    settings = Settings()
    configure_logging(settings)
    return create_app(settings)


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
    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": settings.app_version,
            "deployment.environment": settings.deployment_environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_ratio)),
    )
    exporter = span_exporter if span_exporter is not None else _build_otlp_span_exporter(settings)
    provider.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(exporter)))
    return provider


def _trace_every_request(app: FastAPI, engine: AsyncEngine, provider: TracerProvider) -> None:
    """Request, database, outbound HTTP and agent spans share one global provider, so a
    request's queries and model calls land in its trace (ADR-0030)."""
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        # The container probe calls it every few seconds (ADR-0023).
        excluded_urls=_UNTRACED_URLS,
        # One span per ASGI message would bury the request's real work.
        exclude_spans=["receive", "send"],
    )
    # The engine is async; SQLAlchemy's events fire on the sync engine inside it.
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)
    # OpenRouter, Jev and the external lookups all go through httpx2.
    HTTPX2ClientInstrumentor().instrument(tracer_provider=provider)
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider, include_content=False))


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
