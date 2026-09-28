from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from ai_trainer.application.account import delete_account
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.users import User
from ai_trainer.web.auth import SESSION_COOKIE_NAME


def build_settings_router(templates: Jinja2Templates, *, users: UsersRepositoryPort) -> APIRouter:
    """Wires `GET /settings` (ticket #41) and `POST /settings/delete-account` (ticket #17,
    G7). Training and the rest of Account ship in M1. The theme choice itself lives only in
    the browser (`static/js/theme.js`, ADR-0019); `GET /settings` carries no theme state.
    """
    router = APIRouter()

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_index(request: Request) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        template_name = "partials/settings/index.html" if is_htmx else "pages/settings/index.html"
        return templates.TemplateResponse(request, template_name)

    @router.post("/settings/delete-account")
    async def settings_delete_account(request: Request) -> Response:
        user: User = request.state.user
        await delete_account(user.id, users=users)

        # Same shape as `/auth/logout` (`web/auth.py`): htmx never processes redirect headers
        # on a 3xx, so an htmx-issued request needs `HX-Redirect` on a 2xx instead. Either way
        # the next request lands on "/" with no session cookie, and `ActiveUserGateMiddleware`
        # renders the unauthenticated page in its place.
        response: Response
        if request.headers.get("HX-Request") == "true":
            response = Response(status_code=200, headers={"HX-Redirect": "/"})
        else:
            response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    return router
