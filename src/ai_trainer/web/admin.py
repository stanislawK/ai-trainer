from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates


def build_admin_router(templates: Jinja2Templates) -> APIRouter:
    """Wires `GET /admin` (ticket #40): a stub landing page gated on admin rights derived per
    request (ADR-0005). #16 replaces the page with the real user table; this only proves the
    nav item and the gate.
    """
    router = APIRouter()

    @router.get("/admin", response_class=HTMLResponse)
    async def admin_index(request: Request) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        if not getattr(request.state, "is_admin", False):
            template_name = (
                "partials/admin/forbidden.html" if is_htmx else "pages/admin/forbidden.html"
            )
            return templates.TemplateResponse(request, template_name, status_code=403)

        template_name = "partials/admin/index.html" if is_htmx else "pages/admin/index.html"
        return templates.TemplateResponse(request, template_name)

    return router
