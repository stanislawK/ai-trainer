"""The admin's API tokens (ADR-0036 and its #454 amendment, #455): `GET /admin/tokens` lists the
admin's own tokens, `POST /admin/tokens` creates one and shows it once, and
`POST /admin/tokens/{token_id}/revoke` revokes one. Every response renders the whole `#tokens`
region.

As on the other admin pages, the admin check comes before anything from the request is parsed:
the form and the token ID are read by hand after it, so a non-admin gets 403, never a 422 or 404.
Every response is `no-store`, so the one that shows a new token is never cached, and the token is
never logged."""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.templating import Jinja2Templates

from ai_trainer.application.admin_tokens import AdminTokens, IssuedToken
from ai_trainer.domain.admin_tokens import MAX_TOKEN_DAYS, InvalidTokenLifetimeError
from ai_trainer.web.admin.access import forbidden, is_admin, is_htmx, not_found

# The lifetimes the form offers (the Admin Staging mock); the server takes any whole number of
# days from 1 to `MAX_TOKEN_DAYS`.
LIFETIME_DAYS = (30, 14, 7, 1)


def _days(raw: object) -> int | None:
    # At most two digits, since 30 is the cap: a longer string is refused before `int()`, which
    # raises past 4300 digits.
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 2:
        return None
    return int(raw)


class _TokenRoutes:
    def __init__(self, *, templates: Jinja2Templates, tokens: AdminTokens) -> None:
        self._templates = templates
        self._tokens = tokens

    async def _region(
        self,
        request: Request,
        *,
        status_code: int = 200,
        created: IssuedToken | None = None,
        lifetime_refused: bool = False,
    ) -> HTMLResponse:
        context = {
            "tokens": await self._tokens.list(request.state.user.id),
            "now": self._tokens.now(),
            "created": created,
            "lifetime_refused": lifetime_refused,
            "lifetime_days": LIFETIME_DAYS,
            "max_days": MAX_TOKEN_DAYS,
        }
        template_name = (
            "partials/admin/tokens.html" if is_htmx(request) else "pages/admin/tokens.html"
        )
        response = self._templates.TemplateResponse(
            request, template_name, context, status_code=status_code
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def page(self, request: Request) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        return await self._region(request)

    async def create(self, request: Request) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        async with request.form(max_files=0, max_fields=4) as form:
            days = _days(form.get("days"))
        if days is None:
            return await self._region(request, status_code=422, lifetime_refused=True)
        try:
            issued = await self._tokens.issue(request.state.user.id, days=days)
        except InvalidTokenLifetimeError:
            return await self._region(request, status_code=422, lifetime_refused=True)
        return await self._region(request, created=issued)

    async def revoke(self, request: Request, token_id: str) -> Response:
        if not is_admin(request):
            return forbidden(request, self._templates)
        try:
            parsed = UUID(token_id)
        except ValueError:
            return not_found(request, self._templates)
        if await self._tokens.revoke(request.state.user.id, parsed) is None:
            return not_found(request, self._templates)
        return await self._region(request)


def build_tokens_router(*, templates: Jinja2Templates, tokens: AdminTokens) -> APIRouter:
    routes = _TokenRoutes(templates=templates, tokens=tokens)
    router = APIRouter()
    router.add_api_route("/admin/tokens", routes.page, methods=["GET"], response_class=HTMLResponse)
    router.add_api_route(
        "/admin/tokens", routes.create, methods=["POST"], response_class=HTMLResponse
    )
    router.add_api_route(
        "/admin/tokens/{token_id}/revoke",
        routes.revoke,
        methods=["POST"],
        response_class=HTMLResponse,
    )
    return router
