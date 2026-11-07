"""Composition root: the only place concrete classes are wired (ADR-0003)."""

import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.util.re import parse_env_headers
from pydantic_ai import Agent, InstrumentationSettings
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ai_trainer.adapters.activity_stores import ActivityStores
from ai_trainer.adapters.admin_token_repository import SqlAlchemyAdminTokensRepository
from ai_trainer.adapters.catalogue_knowledge_repository import SqlAlchemyCatalogueChunks
from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.clarification_repository import SqlAlchemyClarificationRepository
from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.exercise_history import SqlAlchemyExerciseHistory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.feedback_repository import SqlAlchemyFeedbackEventsRepository
from ai_trainer.adapters.google_oauth import AuthlibGoogleOAuthClient
from ai_trainer.adapters.gym_activity_store import GymActivityStore
from ai_trainer.adapters.gym_band_order import SqlAlchemyBandOrder
from ai_trainer.adapters.gym_session_reader import SqlAlchemyGymSessionReader
from ai_trainer.adapters.health import PsycopgDatabaseHealth
from ai_trainer.adapters.knowledge_chunk_repository import SqlAlchemyKnowledgeIngestionRepository
from ai_trainer.adapters.knowledge_repository import SqlAlchemyKnowledgeSourcesRepository
from ai_trainer.adapters.knowledge_search_repository import SqlAlchemyKnowledgeSearch
from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.log_records import configure_logging
from ai_trainer.adapters.metrics import OtelMetrics
from ai_trainer.adapters.object_storage import ObstoreObjectStorage
from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.pmc import PmcCloudFiles, PmcCloudMetadata
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.telemetry import ScrubbingSpanExporter
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.admin_tokens import AdminTokens, TokenAuthenticator
from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.application.budgeted_gateways import (
    BudgetedDecisionGateway,
    BudgetedLlmGateway,
    BudgetedQueryEmbeddings,
)
from ai_trainer.application.catalogue_ingestion import CatalogueIngestion
from ai_trainer.application.exercise_candidates import (
    KnowledgeExerciseSearch,
    SearchedExerciseNames,
)
from ai_trainer.application.exercise_lookup import ExerciseFinder
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.application.knowledge_file_staging import FileStaging
from ai_trainer.application.knowledge_ingestion import KnowledgeIngestion
from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.knowledge_sources import KnowledgeSources
from ai_trainer.application.knowledge_staging import KnowledgeStaging
from ai_trainer.application.metered_calls import MeteredLlmCallsRepository
from ai_trainer.application.model_budget import ModelCallBudget
from ai_trainer.application.pmc_source_file import PmcSourceFile
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.conversation import ConversationModelPort
from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort
from ai_trainer.application.ports.draft_settling import DraftSettlerPort
from ai_trainer.application.ports.embeddings import QueryEmbeddingPort
from ai_trainer.application.ports.exercises import ExerciseNamesPort
from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.application.ports.llm_gateway import LlmGatewayPort
from ai_trainer.application.ports.metrics import NO_METRICS, MetricsPort
from ai_trainer.application.ports.object_storage import ObjectStoragePort
from ai_trainer.application.reply import ReplyServices
from ai_trainer.application.session_history import GymSessionHistory
from ai_trainer.application.training_history import ExerciseTrainingHistory, TrainingHistory
from ai_trainer.domain.pmc import PMC_JATS_MAX_BYTES
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.knowledge.chunker import Chunker
from ai_trainer.knowledge.readers import DocumentReaders
from ai_trainer.llm.conversation import LlmConversation, build_prompt_registry
from ai_trainer.llm.debug_log import build_debug_log
from ai_trainer.llm.decision_gateway import OpenRouterDecisionGateway
from ai_trainer.llm.embeddings import OpenRouterEmbeddingGateway
from ai_trainer.llm.exercise_shortlist import JevExercisePicker
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.llm.onboarding import LlmOnboardingReader
from ai_trainer.llm.question_sport import JevQuestionSport
from ai_trainer.llm.router_fast import JevFastRouter, RouterFastThresholds
from ai_trainer.llm.safety_screen import JevSafetyScreen
from ai_trainer.mcp.citations import cited_sources
from ai_trainer.mcp.server import build_mcp_server
from ai_trainer.mcp.toolset import build_toolset
from ai_trainer.settings import Settings
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.ai_onboarding import build_ai_onboarding_router
from ai_trainer.web.api import build_api_router
from ai_trainer.web.auth import build_auth_router
from ai_trainer.web.chat import build_chat_router
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.errors import register_error_handlers
from ai_trainer.web.health import build_health_router
from ai_trainer.web.history import build_history_router
from ai_trainer.web.rate_limit import RateLimitMiddleware
from ai_trainer.web.request_log import log_every_request
from ai_trainer.web.settings import build_settings_router
from ai_trainer.web.sign_in import build_sign_in_router
from ai_trainer.web.templating import STATIC_DIR, build_templates

_PROMPTS_ROOT = Path(__file__).resolve().parent / "llm" / "prompts"

# Bounds a single export attempt (including its retries) so a request that later flushes or
# shuts down the provider is never stalled for long by an unreachable collector (ADR-0018).
_OTLP_EXPORT_TIMEOUT_SECONDS = 5.0

# Grafana Cloud bills active series per minute; a shorter interval only adds data points.
_METRIC_EXPORT_INTERVAL_MS = 60_000

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
        answer_model=settings.answer_model,
        answer_provider=settings.answer_provider_preferences,
        answer_reasoning_effort=settings.answer_reasoning_effort,
        answer_timeout_seconds=settings.answer_call_timeout_seconds,
        answer_max_requests=settings.answer_max_requests,
        extract_model=settings.extract_model,
        extract_provider=settings.extract_provider_preferences,
        extract_timeout_seconds=settings.extract_call_timeout_seconds,
        extract_reasoning_effort=settings.extract_reasoning_effort,
        history_turns=settings.chat_history_turns,
    )


def build_activity_stores(sports: SportRegistry, max_options: int = 5) -> ActivityStores:
    """Where each sport keeps its activities (ADR-0032): its own tables where it has them,
    otherwise the session's payload. `max_options` is `max_clarification_options`, the most
    places a band question offers at once (ticket #431)."""
    return ActivityStores(sports, {"gym": GymActivityStore(max_options=max_options)})


def build_draft_settlers(
    settings: Settings,
    session_factory: Callable[[], AsyncSession],
    conversation: ConversationModelPort,
    decisions: DecisionGatewayPort,
    *,
    sports: SportRegistry,
    calls_repository: LlmCallsRepositoryPort,
    http_client: httpx2.AsyncClient,
    clock: ClockPort,
    budget: ModelCallBudget | None,
) -> Mapping[str, DraftSettlerPort]:
    """What each sport settles on its drafts: the gym's exercise names (ADR-0031, tickets #397
    and #399). "Keep my name" takes one of the card's `max_clarification_options`."""
    return {
        "gym": ResolveExercises(
            build_exercise_names(
                settings,
                session_factory,
                sports,
                calls_repository=calls_repository,
                http_client=http_client,
                clock=clock,
                budget=budget,
            ),
            JevExercisePicker(decisions, threshold=settings.exercise_pick_threshold),
            question=conversation.exercise_question,
            limits=ResolutionLimits(
                near_match=settings.exercise_near_match_threshold,
                candidate_floor=settings.exercise_candidate_floor,
                shortlist_size=min(
                    settings.exercise_shortlist_size, settings.max_clarification_options - 1
                ),
            ),
        )
    }


def build_exercise_names(
    settings: Settings,
    session_factory: Callable[[], AsyncSession],
    sports: SportRegistry,
    *,
    calls_repository: LlmCallsRepositoryPort,
    http_client: httpx2.AsyncClient,
    clock: ClockPort,
    budget: ModelCallBudget | None,
) -> ExerciseNamesPort:
    """The names the gym resolves against: the catalogue and the athlete's own exercises, with
    the catalogue's search as the shortlist's third input when `EXERCISE_SEARCH_ENABLED` is on
    (ADR-0031, #429). The app and the smoke set's live cases build it here, so they can't drift.

    The search's embedding call runs on the log path, outside any tool loop's slot, so the app
    passes its `budget` and the call is charged like a text or decision call (ADR-0029); evals
    pass none, as their gateways are not capped."""
    exercises = SqlAlchemyExerciseRepository(session_factory)
    if not settings.exercise_search_enabled:
        return exercises
    embeddings: QueryEmbeddingPort = build_query_embeddings(
        settings, calls_repository=calls_repository, http_client=http_client
    )
    if budget is not None:
        embeddings = BudgetedQueryEmbeddings(embeddings, budget)
    return SearchedExerciseNames(
        exercises,
        KnowledgeExerciseSearch(
            build_knowledge_search(
                settings,
                session_factory,
                sports,
                calls_repository=calls_repository,
                http_client=http_client,
                embeddings=embeddings,
            ),
            chunks=2 * settings.knowledge_search_depth,
        ),
        exercises,
        timeout_seconds=settings.exercise_search_timeout_seconds,
        name_match=settings.exercise_name_match_floor,
        clock=clock,
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


def build_uploads_storage(settings: Settings) -> ObstoreObjectStorage | None:
    """The `uploads` bucket from `OBJECT_STORAGE_*`, or None while they are unset (ADR-0033):
    the app then lists sources and refuses uploads, and the worker refuses to start."""
    endpoint = settings.object_storage_endpoint
    access_key_id = settings.object_storage_access_key_id
    secret_access_key = settings.object_storage_secret_access_key
    bucket = settings.object_storage_uploads_bucket
    if endpoint is None or access_key_id is None or secret_access_key is None or bucket is None:
        return None
    return ObstoreObjectStorage(
        endpoint=endpoint,
        region=settings.object_storage_region,
        access_key_id=access_key_id.get_secret_value(),
        secret_access_key=secret_access_key.get_secret_value(),
        bucket=bucket,
        timeout_seconds=settings.object_storage_timeout_seconds,
    )


def build_query_embeddings(
    settings: Settings,
    *,
    calls_repository: LlmCallsRepositoryPort,
    http_client: httpx2.AsyncClient,
) -> OpenRouterEmbeddingGateway:
    """The embedding gateway a search query goes through, at `KNOWLEDGE_QUERY_TIMEOUT_SECONDS`
    (shorter than ingestion's)."""
    return OpenRouterEmbeddingGateway(
        api_key=settings.openrouter_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dim,
        provider=settings.embedding_provider_preferences,
        timeout_seconds=settings.knowledge_query_timeout_seconds,
        calls_repository=calls_repository,
        http_client=http_client,
    )


def build_knowledge_search(
    settings: Settings,
    session_factory: Callable[[], AsyncSession],
    sports: SportRegistry,
    *,
    calls_repository: LlmCallsRepositoryPort,
    http_client: httpx2.AsyncClient,
    embeddings: QueryEmbeddingPort | None = None,
) -> KnowledgeSearch:
    """`search_knowledge`'s use case (ADR-0036, #424 amendment). Its query embedding isn't
    charged to ADR-0029's budget separately by default: it runs inside a specialist's tool loop,
    which already holds one slot. A caller outside a loop passes `embeddings` already wrapped in
    the budget (`build_exercise_names`). The caller owns `http_client` and closes it
    (`query_http_client`)."""
    return KnowledgeSearch(
        embeddings=embeddings
        or build_query_embeddings(
            settings, calls_repository=calls_repository, http_client=http_client
        ),
        repository=SqlAlchemyKnowledgeSearch(session_factory),
        sports=sports,
        embedding_model=settings.embedding_model,
        depth=settings.knowledge_search_depth,
    )


def build_app_mcp_server(
    settings: Settings,
    session_factory: Callable[[], AsyncSession],
    sports: SportRegistry,
    *,
    training_sessions: SqlAlchemyTrainingSessionRepository,
    calls_repository: LlmCallsRepositoryPort,
    http_client: httpx2.AsyncClient,
    clock: ClockPort,
) -> FastMCP:
    """The app's MCP server with every tool (ADR-0010), for the specialists' toolset and for
    `scripts/search_knowledge.py`. `http_client` carries the search's embedding calls, from
    `query_http_client`; the caller closes it."""
    exercise_repository = SqlAlchemyExerciseRepository(session_factory)
    exercise_history = SqlAlchemyExerciseHistory(session_factory)
    return build_mcp_server(
        TrainingHistory(training_sessions, sports),
        build_exercise_finder(
            settings,
            session_factory,
            sports,
            calls_repository=calls_repository,
            http_client=http_client,
            clock=clock,
        ),
        ExerciseTrainingHistory(exercise_repository, exercise_history),
        knowledge=build_knowledge_search(
            settings,
            session_factory,
            sports,
            calls_repository=calls_repository,
            http_client=http_client,
        ),
    )


def build_exercise_finder(
    settings: Settings,
    session_factory: Callable[[], AsyncSession],
    sports: SportRegistry,
    *,
    calls_repository: LlmCallsRepositoryPort,
    http_client: httpx2.AsyncClient,
    clock: ClockPort,
) -> ExerciseFinder:
    """`find_exercise`'s use case, shortlisting through the same names a log does, the catalogue
    search included while `EXERCISE_SEARCH_ENABLED` is on (#508). No budget: the tool runs inside
    a specialist's tool loop, which holds one slot (ADR-0031's #429 amendment)."""
    exercise_repository = SqlAlchemyExerciseRepository(session_factory)
    return ExerciseFinder(
        exercise_repository,
        ResolutionLimits(
            near_match=settings.exercise_near_match_threshold,
            candidate_floor=settings.exercise_candidate_floor,
            shortlist_size=settings.exercise_shortlist_size,
        ),
        SqlAlchemyExerciseHistory(session_factory),
        names=build_exercise_names(
            settings,
            session_factory,
            sports,
            calls_repository=calls_repository,
            http_client=http_client,
            clock=clock,
            budget=None,
        ),
    )


def query_http_client(settings: Settings) -> httpx2.AsyncClient:
    """The client for `search_knowledge`'s embedding calls, at `KNOWLEDGE_QUERY_TIMEOUT_SECONDS`."""
    return httpx2.AsyncClient(timeout=settings.knowledge_query_timeout_seconds)


def build_knowledge_ingestion(
    settings: Settings,
    storage: ObjectStoragePort,
    session_factory: Callable[[], AsyncSession],
    *,
    sleep: Callable[[float], Awaitable[None]],
    embedding_transport: httpx2.AsyncBaseTransport | None = None,
    pmc_transport: httpx2.AsyncBaseTransport | None = None,
) -> KnowledgeIngestion:
    """`ai-trainer ingest-worker`'s worker, outside the web process (ADR-0036). Its embedding
    calls are outside ADR-0029's per-user budget, bounded by its concurrency of 1; each still
    writes its `llm_calls` row. An approved PMC article's JATS is fetched from PMC's bucket by
    its PMC ID (#463)."""
    embeddings = OpenRouterEmbeddingGateway(
        api_key=settings.openrouter_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dim,
        provider=settings.embedding_provider_preferences,
        timeout_seconds=settings.embedding_call_timeout_seconds,
        calls_repository=SqlAlchemyLlmCallsRepository(session_factory),
        http_client=httpx2.AsyncClient(
            transport=embedding_transport, timeout=settings.embedding_call_timeout_seconds
        ),
    )
    clock = UtcClock()
    repository = SqlAlchemyKnowledgeIngestionRepository(session_factory)
    return KnowledgeIngestion(
        repository=repository,
        storage=storage,
        pmc=PmcSourceFile(
            files=PmcCloudFiles(
                metadata=PmcCloudMetadata(
                    bucket_url=settings.pmc_bucket_url,
                    timeout_seconds=settings.pmc_timeout_seconds,
                    transport=pmc_transport,
                ),
                bucket_url=settings.pmc_bucket_url,
                timeout_seconds=settings.pmc_jats_timeout_seconds,
                max_bytes=PMC_JATS_MAX_BYTES,
                transport=pmc_transport,
            ),
            storage=storage,
            repository=repository,
        ),
        readers=DocumentReaders(),
        chunker=Chunker(
            max_words=settings.knowledge_chunk_max_words,
            min_words=settings.knowledge_chunk_min_words,
        ),
        embeddings=embeddings,
        clock=clock,
        sleep=sleep,
        batch_size=settings.knowledge_embedding_batch_size,
        max_attempts=settings.knowledge_embedding_max_attempts,
        lease=timedelta(seconds=settings.knowledge_ingest_lease_seconds),
        max_takeovers=settings.knowledge_ingest_max_takeovers,
        catalogue=CatalogueIngestion(
            repository=SqlAlchemyCatalogueChunks(session_factory),
            embeddings=embeddings,
            clock=clock,
            sleep=sleep,
            batch_size=settings.knowledge_embedding_batch_size,
            max_attempts=settings.knowledge_embedding_max_attempts,
            model=settings.embedding_model,
        ),
    )


def build_knowledge_sources(
    settings: Settings,
    session_factory: Callable[[], AsyncSession],
    clock: ClockPort,
    sports: SportRegistry,
) -> KnowledgeSources:
    """The admin's knowledge uploads (ADR-0033). Without the `OBJECT_STORAGE_*` settings the
    page still lists sources and refuses an upload, so a release never waits on the keys."""
    storage = build_uploads_storage(settings)
    return KnowledgeSources(
        sources=SqlAlchemyKnowledgeSourcesRepository(session_factory),
        storage=storage,
        clock=clock,
        max_upload_bytes=settings.knowledge_max_upload_mb * 1024 * 1024,
        sports=[sport.id for sport in sports.all()],
    )


def create_app(  # noqa: PLR0915 -- composition root: objects are built only here (ADR-0003)
    settings: Settings,
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    google_client_kwargs: Mapping[str, object] | None = None,
) -> FastAPI:
    """`span_exporter` and `metric_reader` let tests keep spans and metrics in memory;
    production always exports both over OTLP. `google_client_kwargs` lets a test hand Authlib's
    Google client an in-process transport; production never passes it (#383)."""
    engine = build_engine(str(settings.database_url))
    resource = _build_resource(settings)
    meter_provider = _build_meter_provider(settings, resource, metric_reader)
    metrics: MetricsPort = (
        NO_METRICS
        if meter_provider is None
        else OtelMetrics(meter_provider.get_meter("ai_trainer", settings.app_version))
    )

    # The onboarding's background reads, held so none is collected mid-read (ADR-0028).
    reads = BackgroundReads()
    search_http = query_http_client(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        # A read cut off here stays `pending`; the summary reads it again (ADR-0028).
        await reads.close()
        await search_http.aclose()
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
    activity_stores = build_activity_stores(sport_registry, settings.max_clarification_options)
    llm_calls = MeteredLlmCallsRepository(SqlAlchemyLlmCallsRepository(session_factory), metrics)
    debug_log = build_debug_log(settings, clock)
    # One budget for text calls and decision calls (ADR-0029). Evals build their own gateways.
    budget = ModelCallBudget(
        clock,
        per_hour=settings.llm_calls_per_user_per_hour,
        in_flight=settings.llm_calls_in_flight_per_user,
        metrics=metrics,
    )
    training_sessions = SqlAlchemyTrainingSessionRepository(session_factory, stores=activity_stores)
    # The MCP server in process, which the answer specialists read through (ADR-0010). Nothing
    # mounts it over HTTP: that waits for an amendment on how tokens are issued.
    app.state.mcp_toolset = build_toolset(
        build_app_mcp_server(
            settings,
            session_factory,
            sport_registry,
            training_sessions=training_sessions,
            calls_repository=llm_calls,
            http_client=search_http,
            clock=clock,
        )
    )
    gateway = BudgetedLlmGateway(
        OpenRouterGateway(
            settings,
            llm_calls,
            debug_log=debug_log,
            toolset=app.state.mcp_toolset,
            citations=cited_sources,
        ),
        budget,
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
        drafts=SqlAlchemySessionDraftRepository(
            session_factory, sport_registry, stores=activity_stores
        ),
        clarifications=SqlAlchemyClarificationRepository(session_factory),
        sessions=training_sessions,
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
        # Names the sport of a training question (ADR-0008 amendment at #421's plan gate).
        question_sport=JevQuestionSport(decisions, threshold=settings.question_sport_threshold),
        clock=clock,
        history_turns=settings.chat_history_turns,
        merge_window=timedelta(minutes=settings.session_merge_window_min),
        max_clarification_options=settings.max_clarification_options,
        recent_sessions=settings.extract_recent_sessions,
        metrics=metrics,
        # Content-free feedback events from the athlete's edits, discards, undos and taps
        # (ADR-0034).
        feedback=FeedbackRecorder(SqlAlchemyFeedbackEventsRepository(session_factory), clock=clock),
        settlers=build_draft_settlers(
            settings,
            session_factory,
            conversation,
            decisions,
            sports=app.state.sport_registry,
            calls_repository=llm_calls,
            http_client=search_http,
            clock=clock,
            budget=budget,
        ),
        band_orders={"gym": SqlAlchemyBandOrder(session_factory)},
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
    knowledge = build_knowledge_sources(settings, session_factory, clock, sport_registry)
    admin_tokens = SqlAlchemyAdminTokensRepository(session_factory)
    app.include_router(
        build_admin_router(
            templates=templates,
            users=users,
            sessions=sessions,
            status_changer=status_changer,
            knowledge=knowledge,
            tokens=AdminTokens(tokens=admin_tokens, clock=clock),
            sports=sport_registry,
            max_upload_mb=settings.knowledge_max_upload_mb,
        )
    )
    # The token routes (ADR-0036): bearer only, outside the session gate and CSRF.
    app.include_router(
        build_api_router(
            authenticator=TokenAuthenticator(
                tokens=admin_tokens,
                users=users,
                clock=clock,
                admin_emails=settings.admin_emails,
            ),
            knowledge=knowledge,
            staging=KnowledgeStaging(
                sources=SqlAlchemyKnowledgeSourcesRepository(session_factory),
                pmc=PmcCloudMetadata(
                    bucket_url=settings.pmc_bucket_url,
                    timeout_seconds=settings.pmc_timeout_seconds,
                ),
                clock=clock,
                sports=[sport.id for sport in sport_registry.all()],
            ),
            files=FileStaging(
                sources=SqlAlchemyKnowledgeSourcesRepository(session_factory),
                storage=build_uploads_storage(settings),
                clock=clock,
                max_upload_bytes=settings.knowledge_max_upload_mb * 1024 * 1024,
                sports=[sport.id for sport in sport_registry.all()],
            ),
        )
    )
    app.include_router(
        build_history_router(
            templates, history=GymSessionHistory(SqlAlchemyGymSessionReader(session_factory))
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
        client_kwargs={**(google_client_kwargs or {}), "scope": "openid email profile"},
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
        token_per_minute=settings.token_requests_per_minute,
        token_failures_per_address_per_minute=settings.token_failures_per_address_per_minute,
        metrics=metrics,
    )
    # A request whose Host is not the public origin's is refused with 400 before the session,
    # CSRF or sign-in gate sees it (ADR-0023). The port is not compared, so the host alone is
    # allowed.
    public_host = urlsplit(settings.public_base_url).hostname
    assert public_host is not None  # `Settings` only accepts an origin with a host
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[public_host])
    # Around Starlette's error middleware too, so a refused or failed request also gets a
    # `request_id` and an access line (ADR-0030).
    log_every_request(app, metrics)

    tracer_provider = _build_tracer_provider(settings, span_exporter, resource)
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


def _build_resource(settings: Settings) -> Resource:
    """What every span and metric says it came from (ADR-0030)."""
    return Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": settings.app_version,
            "deployment.environment": settings.deployment_environment,
        }
    )


def _build_tracer_provider(
    settings: Settings,
    span_exporter: SpanExporter | None = None,
    resource: Resource | None = None,
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
    provider = TracerProvider(
        resource=resource or _build_resource(settings),
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_ratio)),
    )
    exporter = span_exporter if span_exporter is not None else _build_otlp_span_exporter(settings)
    provider.add_span_processor(BatchSpanProcessor(ScrubbingSpanExporter(exporter)))
    return provider


def _build_meter_provider(
    settings: Settings, resource: Resource, metric_reader: MetricReader | None = None
) -> MeterProvider | None:
    """The app's own metrics over OTLP, off with tracing (ADR-0030 invariant 5).

    It is not made global, so the FastAPI, httpx and SQLAlchemy instrumentors record no
    metrics of their own: theirs can carry a raw path or a host, and these are the only series
    the dashboard reads (invariant 4). The periodic reader exports on its own thread and logs a
    failed export, so neither an unreachable Alloy nor a failing exporter reaches a request.
    """
    if not settings.otel_enabled:
        return None
    reader = metric_reader or PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=_metrics_endpoint(settings.otel_exporter_otlp_endpoint),
            headers=dict(
                parse_env_headers(settings.otel_exporter_otlp_headers or "", liberal=True)
            ),
            timeout=_OTLP_EXPORT_TIMEOUT_SECONDS,
        ),
        export_interval_millis=_METRIC_EXPORT_INTERVAL_MS,
    )
    return MeterProvider(resource=resource, metric_readers=[reader])


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


_METRICS_EXPORT_PATH = "v1/metrics"


def _metrics_endpoint(base_endpoint: str | None) -> str | None:
    return _signal_endpoint(base_endpoint, _METRICS_EXPORT_PATH)


def _traces_endpoint(base_endpoint: str | None) -> str | None:
    return _signal_endpoint(base_endpoint, _TRACES_EXPORT_PATH)


def _signal_endpoint(base_endpoint: str | None, path: str) -> str | None:
    """Appends a signal's OTLP HTTP path to a configured base endpoint.

    The OTLP exporters only append it themselves when they fall back to reading
    `OTEL_EXPORTER_OTLP_ENDPOINT` from the environment directly; passing `endpoint=`
    explicitly — required to source it from `Settings` rather than read the environment
    outside it — bypasses that. Without this, a bare Grafana Cloud host as documented in
    `.env.example` would get spans and metrics POSTed to the wrong path, failing silently since
    both exporters' background threads swallow export errors (ADR-0018).
    """
    if not base_endpoint:
        return None
    if base_endpoint.rstrip("/").endswith(path):
        return base_endpoint
    return f"{base_endpoint.rstrip('/')}/{path}"
