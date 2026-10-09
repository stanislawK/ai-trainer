from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, PostgresDsn, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_comma_separated(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(_split_comma_separated)]


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
    # The `chitchat` specialist's model (ADR-0008): answers chitchat, unclear and wellbeing
    # messages in the persona. Required, no default, for the same reason as `router_model`.
    chitchat_model: str
    # Evals (ADR-0009): pinned, distinct from the model under test, set explicitly on every
    # eval run with `set_default_judge_model`. No default here — a stable/GA OpenRouter model
    # id is an operator choice, not a code default (`.claude/rules/llm.md`).
    eval_judge_model: str

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
