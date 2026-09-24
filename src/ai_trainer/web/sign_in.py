from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.auth import resolve_authenticated_user
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.users import UserStatus
from ai_trainer.web.auth import SESSION_COOKIE_NAME, parse_session_id


def build_sign_in_router(
    *,
    templates: Jinja2Templates,
    users: UsersRepositoryPort,
    sessions: SessionsRepositoryPort,
    clock: ClockPort,
) -> APIRouter:
    """Wires `GET /sign-in` (ADR-0005, ADR-0019, ticket #43). Exempt from
    `ActiveUserGateMiddleware` (F11), so it resolves the session itself: an already-signed-in
    `ACTIVE` user is sent to `/` instead of seeing the button again; anyone else, including a
    `pending`/`disabled` user or a missing/expired/malformed cookie, gets the sign-in page.
    """
    router = APIRouter()

    @router.get("/sign-in")
    async def sign_in(request: Request) -> Response:
        session_id = parse_session_id(request.cookies.get(SESSION_COOKIE_NAME))
        if session_id is not None:
            user = await resolve_authenticated_user(
                session_id, sessions=sessions, users=users, clock=clock
            )
            if user is not None and user.status is UserStatus.ACTIVE:
                return RedirectResponse(url="/", status_code=303)

        is_htmx = request.headers.get("HX-Request") == "true"
        template_name = "partials/auth/sign_in.html" if is_htmx else "pages/auth/sign_in.html"
        return templates.TemplateResponse(request, template_name)

    return router
