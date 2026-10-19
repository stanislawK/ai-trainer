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


def test_the_router_provider_is_unpinned_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv("ROUTER_MODEL", "test/router-model")
    monkeypatch.delenv("ROUTER_PROVIDER", raising=False)

    assert Settings(_env_file=None).router_provider is None

    monkeypatch.setenv("ROUTER_PROVIDER", "z-ai")

    assert Settings(_env_file=None).router_provider == "z-ai"

    monkeypatch.setenv("ROUTER_PROVIDER", "  ")  # as copied from .env.example

    assert Settings(_env_file=None).router_provider is None


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


def test_chitchat_model_is_read_from_its_own_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv("CHITCHAT_MODEL", "test/chitchat-model")

    assert Settings(_env_file=None).chitchat_model == "test/chitchat-model"


def test_missing_chitchat_model_raises_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.delenv("CHITCHAT_MODEL", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("chitchat_model",)]


def test_extract_model_is_read_from_its_own_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv("EXTRACT_MODEL", "test/extract-model")

    assert Settings(_env_file=None).extract_model == "test/extract-model"


def test_missing_extract_model_raises_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.delenv("EXTRACT_MODEL", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("extract_model",)]


def test_the_extract_provider_is_unpinned_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.delenv("EXTRACT_PROVIDER", raising=False)

    assert Settings(_env_file=None).extract_provider is None

    monkeypatch.setenv("EXTRACT_PROVIDER", "z-ai")

    assert Settings(_env_file=None).extract_provider == "z-ai"

    monkeypatch.setenv("EXTRACT_PROVIDER", "  ")  # as copied from .env.example

    assert Settings(_env_file=None).extract_provider is None


def test_decision_settings_default_to_a_pinned_jev_on_openrouter_and_a_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.delenv("DECISION_MODEL", raising=False)
    monkeypatch.delenv("DECISION_CALL_TIMEOUT_SECONDS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.decision_model == "typesafe/jev-1.13"
    assert settings.decision_call_timeout_seconds == 2.0


@pytest.mark.parametrize("model_id", ["typesafe/jev-1.13", "typesafe/jev-1.13-20260917"])
def test_decision_model_accepts_a_versioned_id(
    monkeypatch: pytest.MonkeyPatch, model_id: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv("DECISION_MODEL", model_id)

    assert Settings(_env_file=None).decision_model == model_id


@pytest.mark.parametrize(
    "model_id",
    ["jev-latest", "~typesafe/jev-latest", "typesafe/jev-preview", "typesafe/jev", ""],
)
def test_decision_model_rejects_an_alias_or_unversioned_id(
    monkeypatch: pytest.MonkeyPatch, model_id: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv("DECISION_MODEL", model_id)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("decision_model",)]


def test_decision_call_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv("DECISION_CALL_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [
        ("decision_call_timeout_seconds",)
    ]


def test_the_plausible_sport_floor_defaults_and_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.delenv("ROUTER_FAST_PLAUSIBLE_SPORT_FLOOR", raising=False)

    assert Settings(_env_file=None).router_fast_plausible_sport_floor == 0.1

    monkeypatch.setenv("ROUTER_FAST_PLAUSIBLE_SPORT_FLOOR", "0.4")

    assert Settings(_env_file=None).router_fast_plausible_sport_floor == 0.4


@pytest.mark.parametrize("floor", ["-0.1", "1.1"])
def test_the_plausible_sport_floor_is_a_probability(
    monkeypatch: pytest.MonkeyPatch, floor: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv("ROUTER_FAST_PLAUSIBLE_SPORT_FLOOR", floor)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [
        ("router_fast_plausible_sport_floor",)
    ]


def test_conversation_retention_defaults_to_fourteen_days(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_keys(monkeypatch)
    monkeypatch.delenv("CONVERSATION_RETENTION_DAYS", raising=False)

    assert Settings(_env_file=None).conversation_retention_days == 14


def test_conversation_retention_can_be_set_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_keys(monkeypatch)
    monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "30")

    assert Settings(_env_file=None).conversation_retention_days == 30


def test_conversation_retention_of_zero_days_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_keys(monkeypatch)
    monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "0")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("conversation_retention_days",)]


def test_session_merge_window_defaults_to_thirty_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_keys(monkeypatch)
    monkeypatch.delenv("SESSION_MERGE_WINDOW_MIN", raising=False)

    assert Settings(_env_file=None).session_merge_window_min == 30


def test_session_merge_window_can_be_set_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_keys(monkeypatch)
    monkeypatch.setenv("SESSION_MERGE_WINDOW_MIN", "45")

    assert Settings(_env_file=None).session_merge_window_min == 45


def test_a_session_merge_window_of_zero_minutes_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required_keys(monkeypatch)
    monkeypatch.setenv("SESSION_MERGE_WINDOW_MIN", "0")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [("session_merge_window_min",)]


@pytest.mark.parametrize("variable", ["ROUTER_REASONING_EFFORT", "EXTRACT_REASONING_EFFORT"])
def test_a_reasoning_effort_is_unset_unless_given(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    field = variable.lower()
    monkeypatch.delenv(variable, raising=False)

    assert getattr(Settings(_env_file=None), field) is None

    monkeypatch.setenv(variable, "  ")  # as copied from .env.example

    assert getattr(Settings(_env_file=None), field) is None

    for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        monkeypatch.setenv(variable, effort)

        assert getattr(Settings(_env_file=None), field) == effort


@pytest.mark.parametrize("variable", ["ROUTER_REASONING_EFFORT", "EXTRACT_REASONING_EFFORT"])
def test_an_unknown_reasoning_effort_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", EVAL_JUDGE_MODEL)
    monkeypatch.setenv(variable, "extreme")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert [error["loc"] for error in excinfo.value.errors()] == [(variable.lower(),)]


def test_a_non_string_reasoning_effort_fails_validation() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, router_reasoning_effort=1)  # type: ignore[arg-type]

    assert ("router_reasoning_effort",) in [error["loc"] for error in excinfo.value.errors()]
