import hmac

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp

from ai_trainer.application.csrf import generate_csrf_token
from ai_trainer.web.auth import SESSION_COOKIE_NAME, parse_session_id

CSRF_HEADER_NAME = "X-CSRF-Token"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class CsrfMiddleware(BaseHTTPMiddleware):
    """Issues and checks the signed CSRF token (ADR-0005 invariant 3, ticket #15).

    Every request gets `request.state.csrf_token` set from its session cookie -- `None` when
    there isn't one -- so a template can expose it without knowing how it's computed. Every
    non-GET request must then carry that same token back via `X-CSRF-Token`; a missing, forged
    or another session's token is rejected before the route ever runs.
    """

    def __init__(self, app: ASGIApp, *, secret_key: bytes, templates: Jinja2Templates) -> None:
        super().__init__(app)
        self._secret_key = secret_key
        self._templates = templates

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        session_id = parse_session_id(request.cookies.get(SESSION_COOKIE_NAME))
        csrf_token = (
            None if session_id is None else generate_csrf_token(session_id, self._secret_key)
        )
        request.state.csrf_token = csrf_token

        if request.method not in _SAFE_METHODS:
            submitted = request.headers.get(CSRF_HEADER_NAME)
            valid = (
                csrf_token is not None
                and submitted is not None
                and hmac.compare_digest(csrf_token, submitted)
            )
            if not valid:
                is_htmx = request.headers.get("HX-Request") == "true"
                template_name = "partials/errors/403.html" if is_htmx else "pages/errors/403.html"
                return self._templates.TemplateResponse(request, template_name, status_code=403)

        return await call_next(request)
