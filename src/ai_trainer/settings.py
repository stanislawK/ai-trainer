from datetime import date
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    Field,
    PostgresDsn,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_trainer.domain.knowledge_document import EMBEDDING_DIMENSIONS
from ai_trainer.domain.llm_calls import ReasoningEffort
from ai_trainer.settings_values import (
    DEFAULT_ANSWER_MODEL,
    DEFAULT_ANSWER_PROVIDER,
    DEFAULT_ANSWER_REASONING_EFFORT,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_ONBOARDING_MODEL,
    DEFAULT_ONBOARDING_PROVIDER,
    DEFAULT_ONBOARDING_REASONING_EFFORT,
    LOCAL_HOSTS,
    OBJECT_STORAGE_KEYS,
    VERSIONED_DECISION_MODEL,
    CallTimeout,
    CommaSeparated,
    ExerciseSearchSettings,
    ProviderPreferencesSetting,
    http_origin,
)


class Settings(ExerciseSearchSettings, BaseSettings):
    """All configuration; the only reader of the environment (ADR-0002)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        frozen=True,
        # A failed check prints its message, never the input, which for a check across settings
        # is every setting, the database URL among them.
        hide_input_in_errors=True,
    )

    database_url: PostgresDsn
    openrouter_api_key: SecretStr
    llm_call_timeout_seconds: float = 30.0
    # How often one address may hit the sign-in routes or fail a token on the knowledge API, one
    # admin token that API, and one session the rest (ADR-0029). Model calls share one budget.
    auth_requests_per_minute: int = Field(default=10, ge=1)
    session_requests_per_minute: int = Field(default=120, ge=1)
    token_requests_per_minute: int = Field(default=120, ge=1)
    token_failures_per_address_per_minute: int = Field(default=10, ge=1)
    llm_calls_per_user_per_hour: int = Field(default=300, ge=1)
    llm_calls_in_flight_per_user: int = Field(default=8, ge=1)
    # Local debugging only (ADR-0018, owner's direction 2026-10-14): when on, every model
    # request the gateways make appends one JSON line, prompt and reply included, under
    # `llm_debug_log_dir`. Never exported to traces; off by default.
    llm_debug_log: bool = False
    llm_debug_log_dir: Path = Path(".llm-debug")
    # Router and every specialist see the same last N chat messages (ADR-0008).
    chat_history_turns: int = Field(default=10, ge=1)
    # A conversation is hidden on read and hard-deleted by `ai-trainer purge-conversations` this
    # many days after its last message (PRD-0004 B26, ADR-0021).
    conversation_retention_days: int = Field(default=14, ge=1)
    # The one dataset commit `ai-trainer import-exercises` reads, the day that pin was fetched
    # (shown in the attribution, G10) and the committed overrides and supplement (ADR-0031).
    exercise_dataset_commit: str = Field(
        default="7455efae41b330c265e7cd4b78dfa848e7ce5ebd", pattern=r"^[0-9a-f]{40}$"
    )
    exercise_dataset_fetched_on: date = date(2026, 10, 31)
    exercise_catalogue_dir: Path = Path("data/catalogue")
    # A typed name this similar (`pg_trgm`) to one of the athlete's aliases is that exercise, with
    # no question (ADR-0031, #397): "bench pres" 0.77 passes, "incline" vs "decline bench" 0.67 not.
    exercise_near_match_threshold: float = Field(default=0.75, ge=0, le=1)
    # A catalogue exercise this similar to a typed name is a candidate; with none, the card
    # offers only "Keep my name". Measured at #397: "RDL" tops out at 0.14, nonsense at 0.06.
    exercise_candidate_floor: float = Field(default=0.25, ge=0, le=1)
    # At most this many candidates on the exercise card (PRD-0010 B37).
    exercise_shortlist_size: int = Field(default=4, ge=1)
    # Jev's pick from the shortlist fills the draft's exercise with no card only at or above this
    # probability (ADR-0031, #399). A miss costs a wrong name the athlete must notice and fix; an
    # abstain costs one tap, so it starts high. It moves on `decision_overridden` (ADR-0034).
    exercise_pick_threshold: float = Field(default=0.9, ge=0, le=1)
    # A log sent within this many minutes of the latest log of the athlete's last session in
    # the same sport is offered as an addition to it (PRD-0004 B16, ADR-0014).
    session_merge_window_min: int = Field(default=30, ge=1)
    # One model per template (ADR-0007). No default: a stable/GA OpenRouter id (never a
    # preview) and its cost are an operator choice, like `eval_judge_model` below.
    router_model: str
    # How OpenRouter picks the router's upstream (ADR-0026 step 4), replacing the single-slug
    # `ROUTER_PROVIDER` pin (#127), whose `order` turned off latency-aware routing. Unset lets
    # OpenRouter balance across every endpoint; `quantizations` or `only` keep the router on
    # one serving stack, which #127 found its evals need.
    router_provider_preferences: ProviderPreferencesSetting = None
    # The router's call timeout, above its measured maximum so a slow but normal answer finishes
    # (G14, ADR-0026); a stuck upstream still fails, later.
    router_call_timeout_seconds: CallTimeout = None
    # The router's reasoning effort, sent as `reasoning.effort` (ADR-0026 step 3). Unset sends
    # none, leaving the model's default (`max` on glm-5.3-flash, OpenRouter `get-model`).
    router_reasoning_effort: ReasoningEffort | None = None
    # The `chitchat` specialist's model (ADR-0008): answers chitchat, unclear and wellbeing
    # messages in the persona. Required, no default, for the same reason as `router_model`.
    chitchat_model: str
    # Every sport's extraction template (`<sport>.extract`, ADR-0006, ADR-0008): turns a log
    # into a session draft. Required, no default, for the same reason as `router_model`.
    extract_model: str
    # The extraction templates' provider preferences and call timeout, like the router's.
    extract_provider_preferences: ProviderPreferencesSetting = None
    extract_call_timeout_seconds: CallTimeout = None
    # The extraction templates' reasoning effort, like `router_reasoning_effort`.
    extract_reasoning_effort: ReasoningEffort | None = None
    # One group for the four `onboarding.<step>` templates, which read answers in the
    # background, off the critical path (ADR-0028). Model, routing and effort have defaults,
    # picked on `onboarding.about_you`'s first eval baseline (#254), so no deploy needs a new
    # key; a blank key keeps them.
    onboarding_model: str = DEFAULT_ONBOARDING_MODEL
    onboarding_provider_preferences: ProviderPreferencesSetting = DEFAULT_ONBOARDING_PROVIDER
    onboarding_call_timeout_seconds: CallTimeout = None
    onboarding_reasoning_effort: ReasoningEffort | None = DEFAULT_ONBOARDING_REASONING_EFFORT
    # One group for the `<sport>.answer` templates, which answer a training question from the
    # athlete's history through the MCP tools (B40). Defaults so no deploy needs a new key; a
    # blank key keeps them. The timeout bounds each wait on the model, a whole tool round
    # included; unset falls back to `llm_call_timeout_seconds`.
    answer_model: str = DEFAULT_ANSWER_MODEL
    answer_provider_preferences: ProviderPreferencesSetting = DEFAULT_ANSWER_PROVIDER
    answer_call_timeout_seconds: CallTimeout = None
    answer_reasoning_effort: ReasoningEffort | None = DEFAULT_ANSWER_REASONING_EFFORT
    # The most model requests one answer's tool loop may make. It counts once against
    # `llm_calls_per_user_per_hour` however many it makes (ADR-0010 amendment at #421's plan
    # gate), so this bounds what one budgeted call can cost. 8 since #428: a cited answer adds
    # `search_knowledge` and `cite_sources` rounds to a history read's three.
    answer_max_requests: int = Field(default=8, ge=2, le=20)
    # Evals (ADR-0009): pinned, distinct from the model under test, set explicitly on every
    # eval run with `set_default_judge_model`. No default here — a stable/GA OpenRouter model
    # id is an operator choice, not a code default (`.claude/rules/llm.md`).
    eval_judge_model: str
    # Jev on OpenRouter's TypeSafe-compatible `/api/v1/systemone` (ADR-0020): a pinned,
    # versioned ID, the one early-availability model allowed as a default because every
    # decision has a fallback. Its calls use `openrouter_api_key`.
    decision_model: str = "typesafe/jev-1.13"
    # Short on purpose: a decision that misses it falls back to the LLM path or the safe side
    # of its gate, and the message pays this wait before that fallback starts.
    decision_call_timeout_seconds: float = Field(default=2.0, gt=0)
    # `safety_screen` (ADR-0020, G3): P(pain) at or above this counts as a pain mention. A miss
    # costs safety, an extra hit only a gentler reply, so it leans low. Calibrated on
    # `evals/datasets/safety_screen.yaml` at #120: every pain case scored 0.71 or more and every
    # literal no-pain case 0.16 or less, so 0.3 keeps a wide margin under the jitter and gives up
    # only figurative or fatigue wording (0.43-0.77). Recorded in its baseline; change it only
    # through `/tune-prompt safety_screen`.
    safety_screen_pain_threshold: float = Field(default=0.3, ge=0, le=1)
    # P(sport) at or above this counts a training question as that sport (`question_sport`,
    # ADR-0008 amendment at #421's plan gate). A miss costs a "not yet" reply; a wrong pick an
    # answer about the wrong sport, so a new set starts on the side of missing (ADR-0034). It
    # moves on feedback, through `/tune-prompt question_sport`.
    question_sport_threshold: float = Field(default=0.7, ge=0, le=1)
    # `router_fast` (ADR-0020 use 1, ADR-0008 amendment): Jev routes a message itself only when
    # P(several requests) is at or below the first, and the picked intent's probability, and a
    # log's sport's, are at or above the others. A miss here costs only the LLM router's call,
    # a wrong pick a misrouted reply, so the margins sit on the wrong-route side. Calibrated on
    # `evals/datasets/router.yaml` at #122: the nearest declined intent 0.75 and every taken log's
    # sport 0.93 or more. P(several) was re-calibrated at #436 on v2's wording: one sport's
    # two-exercise logs score 0.24 or less, and pain with a log or a list across two sports 0.43
    # or more. Two follow-up logs (0.24-0.29) and two same-sport days (0.29) fall under it too,
    # routed right: same-sport intents share one extraction. v1 can't take this value: it scores
    # pain with a log 0.24. Recorded in the `router_fast` baseline; change them only through
    # `/tune-prompt router_fast`.
    router_fast_multi_request_threshold: float = Field(default=0.3, ge=0, le=1)
    router_fast_intent_threshold: float = Field(default=0.85, ge=0, le=1)
    router_fast_sport_threshold: float = Field(default=0.9, ge=0, le=1)
    # Plausible sports (B23, #123): an athlete's sport with P(sport) at or above this stays in
    # play, and the athlete is asked only when two or more do. Dropping a sport they might have
    # meant files a session under the wrong one; keeping an unlikely one only costs a question,
    # so it leans low. Calibrated on `evals/datasets/router.yaml` at #123: sports a message
    # clearly rules out scored 0.07 or less, and the lowest sport it leaves in play 0.16, so
    # 0.1 sits in that gap. Recorded in the `router_fast` baseline; change it only through
    # `/tune-prompt router_fast`.
    router_fast_plausible_sport_floor: float = Field(default=0.1, ge=0, le=1)
    # ADR-0015: a question offers at most this many options, so a longer list of plausible
    # sports is cut to the likeliest.
    max_clarification_options: int = Field(default=5, ge=2)
    # A sport's extraction reads its context from this many of the athlete's latest sessions
    # in that sport, e.g. the climbing kinds they do (ticket #164).
    extract_recent_sessions: int = Field(default=10, ge=1)
    # The private `uploads` bucket on an S3-compatible service (ADR-0033): R2 in production,
    # versitygw under `make start`. R2's region is `auto`; the timeout bounds one request+retries.
    object_storage_endpoint: str | None = None
    object_storage_region: str = "auto"
    object_storage_access_key_id: SecretStr | None = None
    object_storage_secret_access_key: SecretStr | None = None
    object_storage_uploads_bucket: str | None = None
    object_storage_timeout_seconds: float = Field(default=60.0, gt=0)
    # The largest knowledge file an admin may upload, in MiB (ADR-0033). At most 90, so the form
    # fits Caddy's and Cloudflare's 100 MB on that route (ADR-0029).
    knowledge_max_upload_mb: int = Field(default=50, ge=1, le=90)
    # Embeddings (ADR-0033): the model and dimension every chunk records, pinned to one upstream
    # with a one-name `only`, since a chunk doesn't record its upstream. The dimension is the
    # `vector(1024)` column's; another one needs a migration and a re-embed job (ADR-0011).
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dim: int = Field(
        default=EMBEDDING_DIMENSIONS, ge=EMBEDDING_DIMENSIONS, le=EMBEDDING_DIMENSIONS
    )
    embedding_provider_preferences: ProviderPreferencesSetting = DEFAULT_EMBEDDING_PROVIDER
    embedding_call_timeout_seconds: float = Field(default=60.0, gt=0)
    # Ingestion (ADR-0036). Inputs per embedding call, about 13,000 tokens: DeepInfra lists no
    # array cap (`list-model-endpoints`, 2026-11-07, #462). Attempts per batch on 429.
    knowledge_embedding_batch_size: int = Field(default=32, ge=1, le=256)
    knowledge_embedding_max_attempts: int = Field(default=5, ge=1)
    # The chunk budget in words, and the size under which a section merges with a sibling;
    # provisional until the retrieval checks settle them (ADR-0036, #424 amendment).
    knowledge_chunk_max_words: int = Field(default=300, ge=20)
    knowledge_chunk_min_words: int = Field(default=80, ge=0)
    # `search_knowledge` (ADR-0036, #424 amendment): how long a query's embedding call may take
    # before the search fails with a recovery hint, and how deep each leg reads before fusion
    # (pgvector's `rrf.py` reads 20). Both provisional until the retrieval checks.
    knowledge_query_timeout_seconds: float = Field(default=10.0, gt=0)
    knowledge_search_depth: int = Field(default=20, ge=1, le=100)
    # The worker's lease, renewed every third of it; a lapsed one is taken over, so a killed
    # worker's source restarts within about a minute (#462). The idle poll interval. A source
    # taken over more than `max_takeovers` times fails as `worker_crashed`.
    knowledge_ingest_lease_seconds: float = Field(default=60.0, ge=30)
    knowledge_ingest_poll_seconds: float = Field(default=5.0, gt=0)
    knowledge_ingest_max_takeovers: int = Field(default=2, ge=0)
    # `rebuild-knowledge-index`'s maintenance_work_mem, at most the db's 256 MiB shm (#462).
    knowledge_index_build_memory_mb: int = Field(default=256, ge=64, le=256)
    # PMC's Cloud Service bucket, public and anonymous, read by PMC ID when an admin token stages
    # papers (ADR-0036, #458). The timeout bounds one article's two GETs together; at most 10, so
    # a request's 20 articles, five at a time, answer well inside Cloudflare's 100 seconds.
    pmc_bucket_url: str = "https://pmc-oa-opendata.s3.amazonaws.com"
    pmc_timeout_seconds: float = Field(default=8.0, gt=0, le=10)
    # The `ingest` worker's download of an approved article's JATS (#463): one deadline for the
    # file's GET. Longer than the staging request's, since it is off any request and a file can
    # be several megabytes; well under the worker's lease.
    pmc_jats_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    # Google sign-in (ADR-0005). No Google access or refresh token is ever stored.
    # Required and non-blank: a blank value otherwise fails only on Google's own page.
    google_client_id: str
    google_client_secret: SecretStr
    # Signs Starlette's transient OAuth-state cookie; distinct from the app's own
    # PostgreSQL-backed session cookie.
    session_secret_key: SecretStr
    # Signs the CSRF token (ADR-0005 invariant 3): kept separate from `session_secret_key`
    # so the OAuth-state cookie signer and the CSRF token signer don't share a key.
    csrf_secret_key: SecretStr
    # Case-insensitively matched against a verified Google email to bootstrap the first
    # admin (ADR-0005); comma-separated, e.g. "a@example.com,b@example.com".
    admin_emails: CommaSeparated = []
    # The one origin the app is served from (ADR-0023): `http://localhost:8000` locally,
    # `https://<domain>` in production. The OAuth redirect URI is built from it and the
    # trusted-host check admits only its host, so a request's Host header never shapes either.
    public_base_url: str
    session_cookie_secure: bool = True
    session_ttl_days: int = 14

    # Telemetry (ADR-0018): off by default; Grafana Cloud is the intended backend and speaks
    # OTLP/HTTP only, so the protocol is fixed rather than a real switch.
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_protocol: Literal["http/protobuf"] = "http/protobuf"
    otel_exporter_otlp_headers: str | None = None
    otel_service_name: str = "ai-trainer"
    # The share of new traces kept; a request carrying a sampled parent stays sampled (ADR-0030).
    otel_traces_sampler_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    # The trace resource's `service.version` (the release tag) and `deployment.environment`.
    app_version: str = "dev"
    deployment_environment: str = "development"

    # Logs (ADR-0030): JSON lines with trace and request IDs in production, plain text by default.
    log_format: Literal["json", "text"] = "text"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator(
        "google_client_id", "google_client_secret", "session_secret_key", "csrf_secret_key"
    )
    @classmethod
    def _reject_blank(cls, value: str | SecretStr, info: ValidationInfo) -> str | SecretStr:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not raw.strip():
            raise ValueError(f"{(info.field_name or '').upper()} must not be blank")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _origin_only(cls, value: str) -> str:
        origin = http_origin(value)
        if origin is None:
            raise ValueError(
                "PUBLIC_BASE_URL must be an http(s) origin without a path, such as "
                f"http://localhost:8000, not {value!r}"
            )
        return origin

    @field_validator("pmc_bucket_url")
    @classmethod
    def _pmc_bucket_url_is_an_origin(cls, value: str) -> str:
        origin = http_origin(value)
        if origin is None:
            raise ValueError(
                "PMC_BUCKET_URL must be an http(s) origin without a path, such as "
                "https://pmc-oa-opendata.s3.amazonaws.com"
            )
        return origin

    @field_validator("object_storage_endpoint")
    @classmethod
    def _object_storage_endpoint_is_an_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        origin = http_origin(value)
        if origin is None:
            # Never echoes the value: credentials pasted into the URL would reach the log.
            raise ValueError(
                "OBJECT_STORAGE_ENDPOINT must be an http(s) origin without a path or credentials, "
                "such as http://localhost:7070"
            )
        return origin

    @model_validator(mode="after")
    def _object_storage_all_or_nothing(self) -> Self:
        # Unset is fine until the admin upload ticket needs it, so a release never waits on new
        # keys. Half set is a mistake that should stop the app at start, not at an admin's upload.
        missing = [key.upper() for key in OBJECT_STORAGE_KEYS if getattr(self, key) is None]
        if 0 < len(missing) < len(OBJECT_STORAGE_KEYS):
            raise ValueError(f"object storage is only partly set; missing {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def _merge_floor_below_the_budget(self) -> Self:
        if self.knowledge_chunk_min_words >= self.knowledge_chunk_max_words:
            raise ValueError(
                "KNOWLEDGE_CHUNK_MIN_WORDS must be below KNOWLEDGE_CHUNK_MAX_WORDS, or every "
                "section would merge"
            )
        return self

    @model_validator(mode="after")
    def _llm_debug_log_only_on_localhost(self) -> Self:
        # The debug log holds athletes' words (ADR-0018), so it is refused wherever the app is
        # served beyond this machine, and logs may leave the host (ADR-0030, #278).
        if self.llm_debug_log and urlsplit(self.public_base_url).hostname not in LOCAL_HOSTS:
            raise ValueError(
                "LLM_DEBUG_LOG must be off unless PUBLIC_BASE_URL's host is localhost or 127.0.0.1"
            )
        return self

    @field_validator(
        "router_call_timeout_seconds",
        "extract_call_timeout_seconds",
        "onboarding_call_timeout_seconds",
        "answer_call_timeout_seconds",
        "router_reasoning_effort",
        "extract_reasoning_effort",
        "object_storage_endpoint",
        "object_storage_access_key_id",
        "object_storage_secret_access_key",
        "object_storage_uploads_bucket",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Before the type check, so a blank effort or timeout is unset rather than invalid; a
        # non-string is left for that check to refuse.
        return value.strip() or None if isinstance(value, str) else value

    @field_validator(
        "onboarding_model",
        "onboarding_provider_preferences",
        "onboarding_reasoning_effort",
        "answer_model",
        "answer_provider_preferences",
        "answer_reasoning_effort",
        "object_storage_region",
        "object_storage_timeout_seconds",
        "embedding_provider_preferences",
        mode="before",
    )
    @classmethod
    def _blank_setting_is_the_default(cls, value: object, info: ValidationInfo) -> object:
        # `.env.example` lists the onboarding and answer keys blank, so a copied `.env` keeps the
        # picked defaults. A storage key blanked along with the others means the same.
        if isinstance(value, str) and not value.strip():
            assert info.field_name is not None
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("decision_model")
    @classmethod
    def _versioned_decision_model(cls, value: str) -> str:
        if not VERSIONED_DECISION_MODEL.fullmatch(value):
            raise ValueError(
                "DECISION_MODEL must be a versioned Jev ID such as typesafe/jev-1.13, "
                f"not {value!r}"
            )
        return value
