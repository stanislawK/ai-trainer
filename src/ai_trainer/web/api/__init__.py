"""JSON routes for admin tokens (ADR-0036): staging only, under `/api/admin/knowledge/`."""

from fastapi import APIRouter

from ai_trainer.application.admin_tokens import TokenAuthenticator
from ai_trainer.application.knowledge_sources import KnowledgeSources
from ai_trainer.application.knowledge_staging import KnowledgeStaging
from ai_trainer.web.api.knowledge import build_knowledge_api_router


def build_api_router(
    *, authenticator: TokenAuthenticator, knowledge: KnowledgeSources, staging: KnowledgeStaging
) -> APIRouter:
    router = APIRouter()
    router.include_router(
        build_knowledge_api_router(
            authenticator=authenticator, knowledge=knowledge, staging=staging
        )
    )
    return router
