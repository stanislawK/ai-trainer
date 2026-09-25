from collections.abc import Sequence
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from ai_trainer.application.admin import change_user_status
from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.user_status_changer import UserStatusChangerPort
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.users import CannotChangeOwnStatusError, User, UserNotFoundError, UserStatus


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _forbidden(request: Request, templates: Jinja2Templates) -> HTMLResponse:
    template_name = (
        "partials/admin/forbidden.html" if _is_htmx(request) else "pages/admin/forbidden.html"
    )
    return templates.TemplateResponse(request, template_name, status_code=403)


def _pending_count_of(all_users: Sequence[User]) -> int:
    return sum(1 for user in all_users if user.status is UserStatus.PENDING)


def build_admin_router(
    *,
    templates: Jinja2Templates,
    users: UsersRepositoryPort,
    sessions: SessionsRepositoryPort,
    status_changer: UserStatusChangerPort,
) -> APIRouter:
    """Wires the admin user-management page (F7, ADR-0005): `GET /admin` lists every user with
    their status, `POST /admin/users/{user_id}/status` changes one. Admin rights are derived
    per request by `ActiveUserGateMiddleware` (`request.state.is_admin`), never read from the
    database.

    The POST route takes `user_id` and `status` as raw strings and parses them only *after*
    the admin check, rather than typing them `UUID` / `UserStatus` directly: FastAPI validates
    typed path and form parameters before the handler body ever runs, which would otherwise let
    a signed-in non-admin trigger a 422 (bypassing the 403 gate) just by posting a malformed
    value (skeptic finding, ticket #16 review gate)."""
    router = APIRouter()

    @router.get("/admin", response_class=HTMLResponse)
    async def admin_users(request: Request) -> HTMLResponse:
        if not getattr(request.state, "is_admin", False):
            return _forbidden(request, templates)

        all_users = await users.list_all()
        context = {
            "users": all_users,
            "pending_count": _pending_count_of(all_users),
            "total_count": len(all_users),
        }
        template_name = (
            "partials/admin/users.html" if _is_htmx(request) else "pages/admin/users.html"
        )
        return templates.TemplateResponse(request, template_name, context)

    @router.post("/admin/users/{user_id}/status", response_class=HTMLResponse)
    async def admin_update_user_status(
        request: Request, user_id: str, status: str = Form()
    ) -> Response:
        if not getattr(request.state, "is_admin", False):
            return _forbidden(request, templates)

        try:
            target_user_id = UUID(user_id)
            new_status = UserStatus(status)
        except ValueError:
            return Response(status_code=422)

        actor: User = request.state.user
        try:
            updated, previous_status = await change_user_status(
                actor,
                target_user_id,
                new_status,
                status_changer=status_changer,
                sessions=sessions,
            )
        except CannotChangeOwnStatusError:
            return templates.TemplateResponse(
                request, "partials/admin/status_change_error.html", status_code=400
            )
        except UserNotFoundError:
            template_name = (
                "partials/errors/404.html" if _is_htmx(request) else "pages/errors/404.html"
            )
            return templates.TemplateResponse(request, template_name, status_code=404)

        if _is_htmx(request):
            context = {
                "user": updated,
                "previous_status": previous_status,
                "pending_count": _pending_count_of(await users.list_all()),
            }
            return templates.TemplateResponse(request, "partials/admin/user_update.html", context)
        return RedirectResponse(url="/admin", status_code=303)

    return router
