from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates


def build_settings_router(templates: Jinja2Templates) -> APIRouter:
    """Wires `GET /settings` (ticket #41): the Appearance section only. Training, Account and
    the danger zone (#17) ship in M1. The theme choice itself lives only in the browser
    (`static/js/theme.js`, ADR-0019); this route carries no theme state.
    """
    router = APIRouter()

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_index(request: Request) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        template_name = "partials/settings/index.html" if is_htmx else "pages/settings/index.html"
        return templates.TemplateResponse(request, template_name)

    return router
