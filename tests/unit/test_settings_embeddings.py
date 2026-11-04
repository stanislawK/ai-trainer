"""Embedding and ingestion settings (ADR-0033, ADR-0036, #423)."""

import pytest
from pydantic import ValidationError

from ai_trainer.domain.llm_calls import ProviderPreferences
from ai_trainer.settings import Settings


@pytest.fixture(autouse=True)
def _required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "test/judge-model")


def test_the_embedding_defaults_are_adr_0033s_pick_pinned_to_deepinfra() -> None:
    settings = Settings(_env_file=None)

    assert settings.embedding_model == "qwen/qwen3-embedding-8b"
    assert settings.embedding_dim == 1024
    assert settings.embedding_provider_preferences == ProviderPreferences(only=["deepinfra"])
    assert settings.embedding_call_timeout_seconds == 60
    assert settings.knowledge_embedding_batch_size == 32
    assert settings.knowledge_embedding_max_attempts == 5
    assert (settings.knowledge_chunk_max_words, settings.knowledge_chunk_min_words) == (300, 80)
    assert settings.knowledge_ingest_lease_seconds == 300
    assert settings.knowledge_ingest_poll_seconds == 5


def test_a_dimension_other_than_the_columns_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_DIM", "4096")

    with pytest.raises(ValidationError, match="embedding_dim"):
        Settings(_env_file=None)


def test_the_provider_preferences_read_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER_PREFERENCES", '{"only": ["siliconflow"]}')

    assert Settings(_env_file=None).embedding_provider_preferences == ProviderPreferences(
        only=["siliconflow"]
    )


def test_a_blank_provider_preference_keeps_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER_PREFERENCES", "")

    assert Settings(_env_file=None).embedding_provider_preferences == ProviderPreferences(
        only=["deepinfra"]
    )


def test_the_merge_floor_must_be_below_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_CHUNK_MIN_WORDS", "300")

    with pytest.raises(ValidationError, match="KNOWLEDGE_CHUNK_MIN_WORDS"):
        Settings(_env_file=None)


def test_the_heartbeat_lease_has_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_INGEST_LEASE_SECONDS", "5")

    with pytest.raises(ValidationError, match="knowledge_ingest_lease_seconds"):
        Settings(_env_file=None)
