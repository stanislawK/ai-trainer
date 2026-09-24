"""Designed 404 and 500 pages (ADR-0019, ticket #44).

401 (no session) and 403 (bad CSRF token) are rendered directly by their respective
middleware — `active_user_gate.py` and `csrf.py` — since both already have the request in hand
before routing runs. A 404 (no route matched) and an unhandled exception only surface as
Starlette exceptions, so they're wired here as `FastAPI` exception handlers instead.
"""

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.templating import Jinja2Templates

_NOT_FOUND_STATUS = 404
_SERVER_ERROR_STATUS = 500


def register_error_handlers(app: FastAPI, templates: Jinja2Templates) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def not_found_handler(request: Request, exc: StarletteHTTPException) -> Response:
        # Only 404 is restyled here; every other HTTP status (405, a deliberate 400, ...) keeps
        # FastAPI's own default handling, which this ticket doesn't touch.
        if exc.status_code != _NOT_FOUND_STATUS:
            return await http_exception_handler(request, exc)

        is_htmx = request.headers.get("HX-Request") == "true"
        template_name = "partials/errors/404.html" if is_htmx else "pages/errors/404.html"
        return templates.TemplateResponse(request, template_name, status_code=_NOT_FOUND_STATUS)

    @app.exception_handler(Exception)
    async def server_error_handler(request: Request, exc: Exception) -> Response:
        is_htmx = request.headers.get("HX-Request") == "true"
        template_name = "partials/errors/500.html" if is_htmx else "pages/errors/500.html"
        return templates.TemplateResponse(request, template_name, status_code=_SERVER_ERROR_STATUS)
