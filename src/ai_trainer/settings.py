import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BeforeValidator,
    Field,
    PostgresDsn,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from ai_trainer.domain.llm_calls import ProviderPreferences, ReasoningEffort


def _split_comma_separated(value: Any) -> Any:  # noqa: ANN401 -- raw env input of any type
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(_split_comma_separated)]


def _json_object(value: Any) -> Any:  # noqa: ANN401 -- raw env input of any type
    # Decoded here, not by pydantic-settings, so a blank value copied from `.env.example` is
    # unset rather than invalid JSON; the object is then checked field by field.
    if isinstance(value, str):
        return json.loads(value) if value.strip() else None
    return value


# A template's OpenRouter provider preferences as one JSON object, e.g.
# `{"sort": "latency", "preferred_max_latency": {"p90": 2}}` (ADR-0026 step 4).
ProviderPreferencesSetting = Annotated[
    ProviderPreferences | None, NoDecode, BeforeValidator(_json_object)
]
# A template's own call timeout; unset falls back to `llm_call_timeout_seconds`.
CallTimeout = Annotated[float | None, Field(gt=0)]

# Picked on `onboarding.about_you`'s first eval baseline (ADR-0028, #254): 98% of its
# assertions at `low` effort on fp8/nvfp4 upstreams sorted by latency. OpenRouter's default
# price sort lands on an upstream with no structured outputs, which fails the read.
DEFAULT_ONBOARDING_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_ONBOARDING_PROVIDER = ProviderPreferences(sort="latency", quantizations=["fp8", "nvfp4"])
DEFAULT_ONBOARDING_REASONING_EFFORT: ReasoningEffort = "low"

# A Jev ID carries a version number; aliases such as `~typesafe/jev-latest` move under a
# calibrated threshold, so they are refused (ADR-0020).
_VERSIONED_DECISION_MODEL = re.compile(r"^[a-z0-9-]+/jev-\d+\.\d+(\.\d+)?(-\d{8})?$")
# The only hosts `LLM_DEBUG_LOG` may run behind (#278); `localhost.example.com` is public.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


class Settings(BaseSettings):
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
    # How often one address may hit the sign-in routes, and one session the rest of the app
    # (ADR-0029). Model calls share one budget across the text gateway and the decision gateway.
    auth_requests_per_minute: int = Field(default=10, ge=1)
    session_requests_per_minute: int = Field(default=120, ge=1)
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
    # The router's call timeout, set from its measured p95 so a stuck upstream fails fast.
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
    # `router_fast` (ADR-0020 use 1, ADR-0008 amendment): Jev routes a message itself only when
    # P(several requests) is at or below the first, and the picked intent's probability, and a
    # log's sport's, are at or above the others. A miss here costs only the LLM router's call,
    # a wrong pick a misrouted reply, so the margins sit on the wrong-route side. Calibrated on
    # `evals/datasets/router.yaml` at #122: single requests scored P(several) 0.14 or less and
    # the nearest misroute 0.21; the nearest declined intent 0.75 and every taken log's sport
    # 0.93 or more. Recorded in the `router_fast` baseline; change them only through
    # `/tune-prompt router_fast`.
    router_fast_multi_request_threshold: float = Field(default=0.15, ge=0, le=1)
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
        message = (
            "PUBLIC_BASE_URL must be an http(s) origin without a path, such as "
            f"http://localhost:8000, not {value!r}"
        )
        try:
            parts = urlsplit(value.strip())
            host = parts.hostname or ""
            port = parts.port
        except ValueError:
            raise ValueError(message) from None
        # An IPv6 literal or a trailing-dot host would never match the Host header Starlette's
        # trusted-host check compares, locking the app out of every request.
        has_host = bool(host) and ":" not in host and not host.endswith(".") and port != 0
        if (
            parts.scheme not in ("http", "https")
            or not has_host
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError(message)
        return f"{parts.scheme}://{parts.netloc.lower()}"

    @model_validator(mode="after")
    def _llm_debug_log_only_on_localhost(self) -> Self:
        # The debug log holds athletes' words (ADR-0018), so it is refused wherever the app is
        # served beyond this machine, and logs may leave the host (ADR-0030, #278).
        if self.llm_debug_log and urlsplit(self.public_base_url).hostname not in _LOCAL_HOSTS:
            raise ValueError(
                "LLM_DEBUG_LOG must be off unless PUBLIC_BASE_URL's host is localhost or 127.0.0.1"
            )
        return self

    @field_validator(
        "router_call_timeout_seconds",
        "extract_call_timeout_seconds",
        "onboarding_call_timeout_seconds",
        "router_reasoning_effort",
        "extract_reasoning_effort",
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
        mode="before",
    )
    @classmethod
    def _blank_onboarding_setting_is_the_default(
        cls, value: object, info: ValidationInfo
    ) -> object:
        # `.env.example` lists the keys blank, so a copied `.env` keeps the picked defaults.
        if isinstance(value, str) and not value.strip():
            assert info.field_name is not None
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("decision_model")
    @classmethod
    def _versioned_decision_model(cls, value: str) -> str:
        if not _VERSIONED_DECISION_MODEL.fullmatch(value):
            raise ValueError(
                "DECISION_MODEL must be a versioned Jev ID such as typesafe/jev-1.13, "
                f"not {value!r}"
            )
        return value
