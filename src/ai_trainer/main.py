"""Composition root: the only place concrete classes are wired (ADR-0003)."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ai_trainer.adapters.health import PsycopgDatabaseHealth
from ai_trainer.settings import Settings
from ai_trainer.web.health import build_health_router
from ai_trainer.web.home import build_home_router
from ai_trainer.web.templating import STATIC_DIR, build_templates


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="ai-trainer")
    app.state.settings = settings
    health_port = PsycopgDatabaseHealth(str(settings.database_url))
    app.include_router(build_health_router(health_port))
    app.include_router(build_home_router(build_templates()))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn ai_trainer.main:app_factory --factory`."""
    return create_app(Settings())
