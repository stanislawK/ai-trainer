"""Real-socket telemetry behaviour (ADR-0018) — belongs in integration, not unit (ADR-0013)."""

from pydantic import PostgresDsn, SecretStr
from pydantic_ai import Agent, InstrumentationSettings
from pydantic_ai.models.test import TestModel

from ai_trainer.main import _build_tracer_provider
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


async def test_unreachable_otlp_endpoint_does_not_raise_into_the_caller() -> None:
    """AC4: flag on, endpoint unreachable — the agent run still succeeds and a later flush
    of the buffered span (a stand-in for the request's telemetry work) never raises."""
    provider = _build_tracer_provider(
        _settings(otel_enabled=True, otel_exporter_otlp_endpoint=UNREACHABLE_ENDPOINT)
    )
    assert provider is not None
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider, include_content=False))

    result = await Agent(TestModel()).run("hello")

    assert result.output is not None
    provider.force_flush(timeout_millis=6000)
