from fastapi import APIRouter
from starlette.templating import Jinja2Templates

from ai_trainer.application.admin_tokens import AdminTokens
from ai_trainer.application.exercise_media_review import ExerciseMediaReview
from ai_trainer.application.knowledge_sources import KnowledgeSources
from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.user_status_changer import UserStatusChangerPort
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.web.admin.exercises import build_exercises_router
from ai_trainer.web.admin.knowledge import build_knowledge_router
from ai_trainer.web.admin.tokens import build_tokens_router
from ai_trainer.web.admin.users import build_users_router


def build_admin_router(
    *,
    templates: Jinja2Templates,
    users: UsersRepositoryPort,
    sessions: SessionsRepositoryPort,
    status_changer: UserStatusChangerPort,
    knowledge: KnowledgeSources,
    tokens: AdminTokens,
    exercise_media: ExerciseMediaReview,
    sports: SportRegistry,
    max_upload_mb: int,
) -> APIRouter:
    """The admin view, for `ADMIN_EMAILS` users only (ADR-0005): users (F7), knowledge
    sources (B7, ADR-0033), API tokens (ADR-0036) and exercise media (F24, ADR-0031)."""
    router = APIRouter()
    router.include_router(
        build_users_router(
            templates=templates, users=users, sessions=sessions, status_changer=status_changer
        )
    )
    router.include_router(
        build_knowledge_router(
            templates=templates, knowledge=knowledge, sports=sports, max_upload_mb=max_upload_mb
        )
    )
    router.include_router(build_tokens_router(templates=templates, tokens=tokens))
    router.include_router(build_exercises_router(templates=templates, review=exercise_media))
    return router
