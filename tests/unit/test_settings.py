from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_trainer.settings import Settings

ENV_EXAMPLE = Path(__file__).parents[2] / ".env.example"
DATABASE_URL = "postgresql+psycopg://ai_trainer:secret@localhost:5432/ai_trainer"
OPENROUTER_API_KEY = "sk-or-v1-test"
EVAL_JUDGE_MODEL = "test/judge-model"


def test_missing_required_key_raises_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("database_url",)]
    assert "database_url" in str(excinfo.value)


def test_missing_openrouter_api_key_raises_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("openrouter_api_key",)]
    assert "openrouter_api_key" in str(excinfo.value)


GOOGLE_SIGN_IN_KEYS = [
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "SESSION_SECRET_KEY",
    "CSRF_SECRET_KEY",
]


def _set_required_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)


@pytest.mark.parametrize("key", GOOGLE_SIGN_IN_KEYS)
@pytest.mark.parametrize("value", ["", "   "])
def test_blank_google_sign_in_key_raises_naming_it(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    _set_required_keys(monkeypatch)
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [(key.lower(),)]
    assert f"{key} must not be blank" in str(excinfo.value)


@pytest.mark.parametrize("key", GOOGLE_SIGN_IN_KEYS)
def test_missing_google_sign_in_key_raises_naming_it(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    _set_required_keys(monkeypatch)
    monkeypatch.delenv(key)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [(key.lower(),)]


def test_reads_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)

    settings = Settings(_env_file=None)

    assert str(settings.database_url) == DATABASE_URL
    assert settings.openrouter_api_key.get_secret_value() == OPENROUTER_API_KEY
    assert settings.eval_judge_model == EVAL_JUDGE_MODEL
    assert settings.llm_call_timeout_seconds == 30.0
    assert settings.session_cookie_secure is True
    assert settings.session_ttl_days == 14
    assert settings.admin_emails == []


def test_admin_emails_parses_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv("ADMIN_EMAILS", "a@example.com, b@example.com")

    settings = Settings(_env_file=None)

    assert settings.admin_emails == ["a@example.com", "b@example.com"]


def test_env_example_lists_every_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    keys = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert keys == {name.upper() for name in Settings.model_fields}
    # The example leaves the four Google sign-in keys blank for the operator to fill in, which
    # `Settings` rejects on purpose; the environment (set in tests/conftest.py) outranks the file.
    Settings(_env_file=ENV_EXAMPLE)


def test_unknown_key_in_env_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATABASE_URL={DATABASE_URL}\nOPENROUTER_API_KEY={OPENROUTER_API_KEY}\n"
        f"EVAL_JUDGE_MODEL={EVAL_JUDGE_MODEL}\n"
        "DATABSE_POOL_SIZE=5\n"
    )

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=env_file)

    assert [error["loc"] for error in excinfo.value.errors()] == [("databse_pool_size",)]


def test_router_settings_default_to_ten_turns_and_a_named_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)

    monkeypatch.setenv("ROUTER_MODEL", "test/router-model")

    settings = Settings(_env_file=None)

    assert settings.chat_history_turns == 10
    assert settings.router_model == "test/router-model"


def test_missing_router_model_raises_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.delenv("ROUTER_MODEL", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("router_model",)]


def test_chat_history_turns_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv("CHAT_HISTORY_TURNS", "0")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("chat_history_turns",)]
