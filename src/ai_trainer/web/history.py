"""`GET /history`, `GET /history/{session_id}` and `GET`/`POST /history/{session_id}/edit` (PRD
0011 F3, B4, tickets #515 and #516): the athlete's saved gym sessions, newest first, one of them
open beside the list, and its edit form in the same sheet or panel. Saving answers with the
session view; a rejected value is a 422 with the form as typed and that field marked, and
nothing saved. Progress and delete are #517 and #518. An unknown id, or another athlete's, is a
404 (ADR-0021)."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.session_history import GymSessionEdit, GymSessionHistory
from ai_trainer.domain.history import (
    HistorySession,
    SessionChangedError,
    SessionEditError,
    SessionNotEditableError,
)
from ai_trainer.domain.training import SessionNotFoundError
from ai_trainer.domain.users import User
from ai_trainer.web.chat.context import CONFLICT, NOT_FOUND, UNPROCESSABLE, is_htmx, parse_id
from ai_trainer.web.drafts import form_changes


def _opened(sessions: Sequence[HistorySession], wanted: UUID) -> HistorySession:
    opened = next((session for session in sessions if session.id == wanted), None)
    if opened is None:
        raise HTTPException(status_code=NOT_FOUND)
    return opened


def build_history_router(
    templates: Jinja2Templates, *, history: GymSessionHistory, editing: GymSessionEdit
) -> APIRouter:
    router = APIRouter()

    def render(
        request: Request,
        sessions: Sequence[HistorySession],
        opened: HistorySession | None,
        *,
        edit: bool = False,
        values: Mapping[str, str] | None = None,
        errors: frozenset[str] = frozenset(),
    ) -> HTMLResponse:
        name = "partials/history/index.html" if is_htmx(request) else "pages/history/index.html"
        return templates.TemplateResponse(
            request,
            name,
            {
                "sessions": sessions,
                "open": opened,
                "editing": edit,
                "values": values or {},
                "errors": errors,
            },
            status_code=UNPROCESSABLE if errors else 200,
        )

    @router.get("/history", response_class=HTMLResponse)
    async def history_index(request: Request) -> HTMLResponse:
        user: User = request.state.user
        return render(request, await history.list(user.id, user.timezone), None)

    @router.get("/history/{session_id}", response_class=HTMLResponse)
    async def history_session(request: Request, session_id: str) -> HTMLResponse:
        user: User = request.state.user
        sessions = await history.list(user.id, user.timezone)
        return render(request, sessions, _opened(sessions, parse_id(session_id)))

    @router.get("/history/{session_id}/edit", response_class=HTMLResponse)
    async def history_edit(request: Request, session_id: str) -> HTMLResponse:
        """The session's Edit: its exercises and sets as a form in the same sheet or panel."""
        user: User = request.state.user
        sessions = await history.list(user.id, user.timezone)
        return render(request, sessions, _opened(sessions, parse_id(session_id)), edit=True)

    @router.post("/history/{session_id}/edit", response_class=HTMLResponse)
    async def history_save(request: Request, session_id: str) -> HTMLResponse:
        """The edit form's Save (B4): replaces the session's sets and answers with its view,
        pushing its URL. The form's hidden `version` is the session's `updated_at` as rendered.
        A session whose rows no longer read, or that changed since (or a form without its
        `version`), is a 409 and nothing is written."""
        user: User = request.state.user
        wanted = parse_id(session_id)
        form = await request.form()
        values = {key: value for key, value in form.multi_items() if isinstance(value, str)}
        fields = {key: value for key, value in values.items() if key != "version"}
        try:
            await editing.save(
                user.id, wanted, form_changes(fields.items()), version=values.get("version")
            )
        except SessionNotFoundError:
            raise HTTPException(status_code=NOT_FOUND) from None
        except SessionNotEditableError, SessionChangedError:
            raise HTTPException(status_code=CONFLICT) from None
        except SessionEditError as error:
            sessions = await history.list(user.id, user.timezone)
            return render(
                request,
                sessions,
                _opened(sessions, wanted),
                edit=True,
                values=values,
                errors=error.fields,
            )
        sessions = await history.list(user.id, user.timezone)
        response = render(request, sessions, _opened(sessions, wanted))
        response.headers["HX-Push-Url"] = f"/history/{wanted}"
        return response

    return router
