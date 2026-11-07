"""`GET /history` and `GET /history/{session_id}` (PRD 0011 F3, B4, ticket #515): the athlete's
saved gym sessions, newest first, and one of them open beside the list. Read only: edit,
progress and delete are #516, #517 and #518. An unknown id, or another athlete's, is a 404
(ADR-0021)."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.session_history import GymSessionHistory
from ai_trainer.domain.users import User
from ai_trainer.web.chat.context import NOT_FOUND, is_htmx, parse_id


def build_history_router(templates: Jinja2Templates, *, history: GymSessionHistory) -> APIRouter:
    router = APIRouter()

    def render(request: Request, context: dict[str, object]) -> HTMLResponse:
        name = "partials/history/index.html" if is_htmx(request) else "pages/history/index.html"
        return templates.TemplateResponse(request, name, context)

    @router.get("/history", response_class=HTMLResponse)
    async def history_index(request: Request) -> HTMLResponse:
        user: User = request.state.user
        sessions = await history.list(user.id, user.timezone)
        return render(request, {"sessions": sessions, "open": None})

    @router.get("/history/{session_id}", response_class=HTMLResponse)
    async def history_session(request: Request, session_id: str) -> HTMLResponse:
        user: User = request.state.user
        opened = await history.get(user.id, user.timezone, parse_id(session_id))
        if opened is None:
            raise HTTPException(status_code=NOT_FOUND)
        sessions = await history.list(user.id, user.timezone)
        return render(request, {"sessions": sessions, "open": opened})

    return router
