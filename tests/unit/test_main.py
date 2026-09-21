from fastapi import FastAPI
from pydantic import PostgresDsn

from ai_trainer.main import create_app
from ai_trainer.settings import Settings


def test_create_app_wires_settings_into_app() -> None:
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn(
            "postgresql+psycopg://ai_trainer:secret@localhost:5432/ai_trainer"
        ),
    )

    app = create_app(settings)

    assert isinstance(app, FastAPI)
    assert app.state.settings is settings
