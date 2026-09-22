from collections.abc import Sequence
from datetime import UTC, timedelta
from typing import Protocol
from uuid import UUID

from authlib.integrations.base_client import OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ai_trainer.application.auth import sign_in_with_google, sign_out
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.users import GoogleClaims

SESSION_COOKIE_NAME = "session_id"


class GoogleOAuthClient(Protocol):
    """The web-layer's view of a Google OAuth client (ADR-0005): Starlette request/response
    types are inherent to the OAuth redirect dance, so this stays local to `web/`."""

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> RedirectResponse: ...

    async def authorize_access_token(self, request: Request) -> GoogleClaims: ...


def build_auth_router(
    *,
    oauth_client: GoogleOAuthClient,
    users: UsersRepositoryPort,
    sessions: SessionsRepositoryPort,
    clock: ClockPort,
    admin_emails: Sequence[str],
    session_ttl: timedelta,
    cookie_secure: bool,
) -> APIRouter:
    """Wires `/auth/login`, `/auth/callback` and `/auth/logout` (ADR-0005).

    All three are exempt from the active-session gate (ADR-0005 invariant 2, ticket #14).
    """
    router = APIRouter(prefix="/auth")

    @router.get("/login")
    async def login(request: Request) -> RedirectResponse:
        redirect_uri = str(request.url_for("auth_callback"))
        return await oauth_client.authorize_redirect(request, redirect_uri)

    @router.get("/callback", name="auth_callback")
    async def callback(request: Request) -> RedirectResponse:
        try:
            claims = await oauth_client.authorize_access_token(request)
        except OAuthError as exc:
            raise HTTPException(status_code=400, detail="invalid oauth state") from exc

        _user, session = await sign_in_with_google(
            claims,
            admin_emails=admin_emails,
            session_ttl=session_ttl,
            users=users,
            sessions=sessions,
            clock=clock,
        )
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            str(session.id),
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            # `Response.set_cookie` requires exactly `datetime.timezone.utc`; a value read back
            # from Postgres carries an equivalent but distinct `ZoneInfo("Etc/UTC")` tzinfo.
            expires=session.expires_at.astimezone(UTC),
        )
        return response

    @router.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        raw_session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if raw_session_id is not None:
            try:
                session_id = UUID(raw_session_id)
            except ValueError:
                session_id = None
            if session_id is not None:
                await sign_out(session_id, sessions)
        response = RedirectResponse(url="/", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    return router
