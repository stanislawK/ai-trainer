import re
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, PostgresDsn, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_comma_separated(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(_split_comma_separated)]

# A Jev ID carries a version number; aliases such as `~typesafe/jev-latest` move under a
# calibrated threshold, so they are refused (ADR-0020).
_VERSIONED_DECISION_MODEL = re.compile(r"^[a-z0-9-]+/jev-\d+\.\d+(\.\d+)?(-\d{8})?$")


class Settings(BaseSettings):
    """All configuration; the only reader of the environment (ADR-0002)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        frozen=True,
    )

    database_url: PostgresDsn
    openrouter_api_key: SecretStr
    llm_call_timeout_seconds: float = 30.0
    # Router and every specialist see the same last N chat messages (ADR-0008).
    chat_history_turns: int = Field(default=10, ge=1)
    # One model per template (ADR-0007). No default: a stable/GA OpenRouter id (never a
    # preview) and its cost are an operator choice, like `eval_judge_model` below.
    router_model: str
    # The OpenRouter upstream slug the router prefers (e.g. "z-ai"), fallbacks still allowed.
    # Unset lets OpenRouter balance across every endpoint, which made one prompt route the
    # same message differently from call to call (#127).
    router_provider: str | None = None
    # The `chitchat` specialist's model (ADR-0008): answers chitchat, unclear and wellbeing
    # messages in the persona. Required, no default, for the same reason as `router_model`.
    chitchat_model: str
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
    session_cookie_secure: bool = True
    session_ttl_days: int = 14

    # Telemetry (ADR-0018): off by default; Grafana Cloud is the intended backend and speaks
    # OTLP/HTTP only, so the protocol is fixed rather than a real switch.
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_protocol: Literal["http/protobuf"] = "http/protobuf"
    otel_exporter_otlp_headers: str | None = None
    otel_service_name: str = "ai-trainer"

    @field_validator(
        "google_client_id", "google_client_secret", "session_secret_key", "csrf_secret_key"
    )
    @classmethod
    def _reject_blank(cls, value: str | SecretStr, info: ValidationInfo) -> str | SecretStr:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not raw.strip():
            raise ValueError(f"{(info.field_name or '').upper()} must not be blank")
        return value

    @field_validator("router_provider")
    @classmethod
    def _blank_is_unset(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("decision_model")
    @classmethod
    def _versioned_decision_model(cls, value: str) -> str:
        if not _VERSIONED_DECISION_MODEL.fullmatch(value):
            raise ValueError(
                "DECISION_MODEL must be a versioned Jev ID such as typesafe/jev-1.13, "
                f"not {value!r}"
            )
        return value
