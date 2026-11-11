"""The picture command's settings (ADR-0031 #524 and *Picture generation*, #533)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from ai_trainer.settings import Settings
from ai_trainer.settings_values import image_model_source_ref


@pytest.fixture(autouse=True)
def _required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "test/judge-model")


def test_the_defaults_are_adr_0031s_model_and_price_with_a_cap_for_one_picture() -> None:
    settings = Settings(_env_file=None)

    assert settings.image_generation_model == "google/gemini-3.1-flash-lite-image"
    assert image_model_source_ref(settings.image_generation_model) == (
        "google/gemini-3.1-flash-lite-image-20260630"
    )
    assert image_model_source_ref("other/image-model") == "other/image-model"
    assert settings.image_generation_price_usd == Decimal("0.0336")
    assert settings.image_generation_spend_cap_usd == Decimal("0.25")
    assert settings.image_generation_timeout_seconds == 120


@pytest.mark.parametrize(
    "key",
    [
        "IMAGE_GENERATION_MODEL",
        "IMAGE_GENERATION_PRICE_USD",
        "IMAGE_GENERATION_SPEND_CAP_USD",
        "IMAGE_GENERATION_TIMEOUT_SECONDS",
    ],
)
def test_a_blank_key_keeps_the_default(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    field = key.lower()
    default = Settings.model_fields[field].default
    monkeypatch.setenv(key, " ")

    assert getattr(Settings(_env_file=None), field) == default


@pytest.mark.parametrize(
    "key",
    [
        "IMAGE_GENERATION_PRICE_USD",
        "IMAGE_GENERATION_SPEND_CAP_USD",
        "IMAGE_GENERATION_TIMEOUT_SECONDS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_price_cap_or_timeout_must_be_above_zero(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match=key.lower()):
        Settings(_env_file=None)


def test_the_cap_reads_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_SPEND_CAP_USD", "1.50")

    assert Settings(_env_file=None).image_generation_spend_cap_usd == Decimal("1.50")


def test_a_cap_below_one_pictures_price_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """No run could ever draw: the image call alone would pass the cap."""
    monkeypatch.setenv("IMAGE_GENERATION_PRICE_USD", "0.05")
    monkeypatch.setenv("IMAGE_GENERATION_SPEND_CAP_USD", "0.04")

    with pytest.raises(ValidationError, match="IMAGE_GENERATION_SPEND_CAP_USD"):
        Settings(_env_file=None)


def test_a_cap_equal_to_one_pictures_price_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_PRICE_USD", "0.05")
    monkeypatch.setenv("IMAGE_GENERATION_SPEND_CAP_USD", "0.05")

    assert Settings(_env_file=None).image_generation_spend_cap_usd == Decimal("0.05")
