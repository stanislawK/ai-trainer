import pytest
from fastapi import FastAPI
from pydantic import PostgresDsn

from ai_trainer.main import app_factory, create_app
from ai_trainer.settings import Settings

DATABASE_URL = "postgresql+psycopg://ai_trainer:secret@localhost:5432/ai_trainer"


def test_create_app_wires_settings_into_app() -> None:
    settings = Settings(_env_file=None, database_url=PostgresDsn(DATABASE_URL))

    app = create_app(settings)

    assert isinstance(app, FastAPI)
    assert app.state.settings is settings


def test_app_factory_wires_settings_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)

    app = app_factory()

    assert isinstance(app, FastAPI)
    assert str(app.state.settings.database_url) == DATABASE_URL
