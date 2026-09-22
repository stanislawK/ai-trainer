from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates


def build_home_router(templates: Jinja2Templates) -> APIRouter:
    """Wires `GET /`: a full page, or a partial for htmx requests (ADR-0012)."""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        template_name = "partials/home_content.html" if is_htmx else "pages/home.html"
        return templates.TemplateResponse(request, template_name)

    return router
