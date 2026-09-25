from collections.abc import Sequence

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp

from ai_trainer.application.auth import resolve_authenticated_user
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.users import UserStatus, is_admin_email
from ai_trainer.web.auth import SESSION_COOKIE_NAME, parse_session_id

# Every route except sign-in, the callback, sign-out, health and static assets requires an
# `active` user (ADR-0005 invariants 2 and 7). The status screen itself needs no exemption:
# it is rendered in place of the requested route, never at a route of its own. `/sign-in`
# resolves its own session and decides for itself whether to redirect (ticket #43).
_EXEMPT_PATHS = frozenset({"/auth/login", "/auth/callback", "/auth/logout", "/health", "/sign-in"})
_EXEMPT_PREFIXES = ("/static/",)


def _is_exempt(path: str) -> bool:
    # FastAPI's own router redirects a trailing-slash request (e.g. a health probe hitting
    # `/health/`) to its canonical path, but that redirect only happens once routing is
    # reached — downstream of this middleware. Comparing the un-trailed path here keeps a
    # trailing slash on an otherwise-exempt route from being refused before it ever gets
    # that chance.
    canonical = path.rstrip("/") or "/"
    return canonical in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)


class ActiveUserGateMiddleware(BaseHTTPMiddleware):
    """Resolves the session and its user fresh on every request (ticket #14).

    An unauthenticated request to a non-exempt route is refused outright. A `pending` or
    `disabled` user gets the neutral status screen rendered in place of the requested route,
    so a status change in the database takes effect on that user's next request with no new
    sign-in and no separate URL to redirect through.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        users: UsersRepositoryPort,
        sessions: SessionsRepositoryPort,
        clock: ClockPort,
        templates: Jinja2Templates,
        admin_emails: Sequence[str],
    ) -> None:
        super().__init__(app)
        self._users = users
        self._sessions = sessions
        self._clock = clock
        self._templates = templates
        self._admin_emails = admin_emails

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if _is_exempt(request.url.path):
            return await call_next(request)

        session_id = parse_session_id(request.cookies.get(SESSION_COOKIE_NAME))
        user = (
            None
            if session_id is None
            else await resolve_authenticated_user(
                session_id, sessions=self._sessions, users=self._users, clock=self._clock
            )
        )
        is_htmx = request.headers.get("HX-Request") == "true"
        if user is None:
            template_name = "partials/errors/401.html" if is_htmx else "pages/errors/401.html"
            return self._templates.TemplateResponse(request, template_name, status_code=401)

        if user.status is not UserStatus.ACTIVE:
            template_name = "partials/auth/status.html" if is_htmx else "pages/auth/status.html"
            return self._templates.TemplateResponse(
                request, template_name, {"status": user.status.value, "email": user.email}
            )

        request.state.user = user
        request.state.is_admin = is_admin_email(user.email, self._admin_emails)
        return await call_next(request)
