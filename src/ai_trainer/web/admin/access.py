from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def is_admin(request: Request) -> bool:
    """Derived per request by `ActiveUserGateMiddleware` from `ADMIN_EMAILS` (ADR-0005), never
    read from the database."""
    return bool(getattr(request.state, "is_admin", False))


def forbidden(request: Request, templates: Jinja2Templates) -> HTMLResponse:
    template_name = (
        "partials/admin/forbidden.html" if is_htmx(request) else "pages/admin/forbidden.html"
    )
    return templates.TemplateResponse(request, template_name, status_code=403)


def not_found(request: Request, templates: Jinja2Templates) -> HTMLResponse:
    template_name = "partials/errors/404.html" if is_htmx(request) else "pages/errors/404.html"
    return templates.TemplateResponse(request, template_name, status_code=404)
