from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ai_trainer.application.health import check_database_health
from ai_trainer.application.ports.health import DatabaseHealthPort


class HealthResponse(BaseModel):
    database: Literal["ok", "unavailable"]


def build_health_router(port: DatabaseHealthPort) -> APIRouter:
    """Wires the given port into a `/health` route (needs no session, ADR-0005)."""
    router = APIRouter()

    @router.get("/health")
    async def health() -> HealthResponse:
        healthy = await check_database_health(port)
        return HealthResponse(database="ok" if healthy else "unavailable")

    return router
