"""Wires the chat, the main screen: `GET /` and `POST /messages` (PRD-0003 F1, B13, ADR-0008,
ticket #78). `user_id` comes only from the authenticated session (ADR-0005)."""

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.chat import (
    MAX_MESSAGE_LENGTH,
    EmptyMessageError,
    MessageTooLongError,
    list_history,
    send_message,
)
from ai_trainer.application.ports.chat import ChatRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.domain.users import User

_UNPROCESSABLE = 422
_MORNING_FROM = 5
_AFTERNOON_FROM = 12
_EVENING_FROM = 18


def _part_of_day(hour: int) -> str:
    if _MORNING_FROM <= hour < _AFTERNOON_FROM:
        return "morning"
    if _AFTERNOON_FROM <= hour < _EVENING_FROM:
        return "afternoon"
    return "evening"


def build_chat_router(
    templates: Jinja2Templates, *, chat: ChatRepositoryPort, clock: ClockPort
) -> APIRouter:
    router = APIRouter()

    def is_htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    def composer(request: Request, *, text: str = "", error: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "partials/chat/composer.html",
            {"text": text, "error": error, "max_length": MAX_MESSAGE_LENGTH},
            status_code=_UNPROCESSABLE if error else 200,
        )

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        user: User = request.state.user
        history = await list_history(user.id, repository=chat)
        local_hour = clock.now().astimezone(ZoneInfo(user.timezone)).hour
        first_name = user.name.split()[0] if user.name and user.name.strip() else None
        context = {
            "history": history,
            "part_of_day": _part_of_day(local_hour),
            "first_name": first_name,
            "text": "",
            "error": None,
            "max_length": MAX_MESSAGE_LENGTH,
        }
        template_name = "partials/chat/index.html" if is_htmx(request) else "pages/chat/index.html"
        return templates.TemplateResponse(request, template_name, context)

    @router.post("/messages", response_class=HTMLResponse)
    async def post_message(request: Request) -> HTMLResponse:
        user: User = request.state.user
        form = await request.form()
        raw = form.get("message", "")
        text = raw if isinstance(raw, str) else ""
        try:
            message = await send_message(user.id, text, clock=clock, repository=chat)
        except EmptyMessageError:
            return composer(request, error="empty")
        except MessageTooLongError:
            return composer(request, text=text, error="too_long")
        return templates.TemplateResponse(
            request,
            "partials/chat/sent.html",
            {"message": message, "text": "", "error": None, "max_length": MAX_MESSAGE_LENGTH},
        )

    return router
