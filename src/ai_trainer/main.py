"""Composition root: the only place concrete classes are wired (ADR-0003)."""

from fastapi import FastAPI

from ai_trainer.settings import Settings


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="ai-trainer")
    app.state.settings = settings
    return app
