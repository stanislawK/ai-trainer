from typing import Protocol

from fastapi import Request
from fastapi.responses import RedirectResponse

from ai_trainer.domain.users import GoogleClaims


class StarletteOAuth2AppLike(Protocol):
    """The subset of Authlib's `StarletteOAuth2App` this adapter depends on."""

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> RedirectResponse: ...

    async def authorize_access_token(self, request: Request) -> dict[str, object]: ...


class AuthlibGoogleOAuthClient:
    """Wraps Authlib's Starlette Google client (ADR-0005), validating its claims at the
    boundary into `GoogleClaims` so no raw token dict — and no access/refresh token — ever
    leaves this adapter."""

    def __init__(self, app: StarletteOAuth2AppLike) -> None:
        self._app = app

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> RedirectResponse:
        return await self._app.authorize_redirect(request, redirect_uri)

    async def authorize_access_token(self, request: Request) -> GoogleClaims:
        token = await self._app.authorize_access_token(request)
        return GoogleClaims.model_validate(token["userinfo"])
