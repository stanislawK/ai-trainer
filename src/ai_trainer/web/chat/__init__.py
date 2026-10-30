"""Wires the chat, the main screen, from its feature sub-routers (ticket #330): the page and its
composer (`thread`), the streamed reply (`reply`), the session draft cards (`drafts`) and the
choice cards (`clarifications`). `user_id` comes only from the authenticated session (ADR-0005),
and an ID from the URL or the form is always looked up with it: another user's conversation,
message, draft or question is a 404."""

from fastapi import APIRouter
from starlette.templating import Jinja2Templates

from ai_trainer.application.ports.chat import ChatRepositoryPort
from ai_trainer.application.reply import ReplyServices
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.web.chat.clarifications import build_clarifications_router
from ai_trainer.web.chat.context import ChatContext
from ai_trainer.web.chat.drafts import build_drafts_router
from ai_trainer.web.chat.reply import build_reply_router
from ai_trainer.web.chat.thread import build_thread_router
from ai_trainer.web.drafts import build_failed_draft_sport
from ai_trainer.web.glyphs import build_reply_glyph


def build_chat_router(
    templates: Jinja2Templates,
    *,
    chat: ChatRepositoryPort,
    replies: ReplyServices,
    sports: SportRegistry,
    retention_days: int,
) -> APIRouter:
    templates.env.globals["reply_glyph"] = build_reply_glyph(sports)
    templates.env.globals["failed_draft_sport"] = build_failed_draft_sport(sports)
    ctx = ChatContext(
        templates=templates,
        chat=chat,
        replies=replies,
        sports=sports,
        retention_days=retention_days,
    )
    router = APIRouter()
    # In this order, so the routes and the OpenAPI document stay as they were.
    router.include_router(build_thread_router(ctx))
    router.include_router(build_reply_router(ctx))
    router.include_router(build_drafts_router(ctx))
    router.include_router(build_clarifications_router(ctx))
    return router
