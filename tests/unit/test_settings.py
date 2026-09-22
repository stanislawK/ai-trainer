from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_trainer.settings import Settings

ENV_EXAMPLE = Path(__file__).parents[2] / ".env.example"
DATABASE_URL = "postgresql+psycopg://ai_trainer:secret@localhost:5432/ai_trainer"
OPENROUTER_API_KEY = "sk-or-v1-test"


def test_missing_required_key_raises_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("database_url",)]
    assert "database_url" in str(excinfo.value)


def test_missing_openrouter_api_key_raises_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("openrouter_api_key",)]
    assert "openrouter_api_key" in str(excinfo.value)


def test_reads_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)

    settings = Settings(_env_file=None)

    assert str(settings.database_url) == DATABASE_URL
    assert settings.openrouter_api_key.get_secret_value() == OPENROUTER_API_KEY
    assert settings.llm_call_timeout_seconds == 30.0


def test_env_example_lists_every_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    keys = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert keys == {name.upper() for name in Settings.model_fields}
    Settings(_env_file=ENV_EXAMPLE)


def test_unknown_key_in_env_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATABASE_URL={DATABASE_URL}\nOPENROUTER_API_KEY={OPENROUTER_API_KEY}\n"
        "DATABSE_POOL_SIZE=5\n"
    )

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=env_file)

    assert [error["loc"] for error in excinfo.value.errors()] == [("databse_pool_size",)]
