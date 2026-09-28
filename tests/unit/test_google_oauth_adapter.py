import pytest
from authlib.integrations.base_client import OAuthError
from fastapi import Request
from fastapi.responses import RedirectResponse

from ai_trainer.adapters.google_oauth import AuthlibGoogleOAuthClient


class _IdTokenRejectedError(Exception):
    """Stands in for Authlib's `JoseError` (e.g. an `iss`/`aud` mismatch), whose module is
    deprecated to import."""


class StubOAuth2App:
    def __init__(
        self,
        token: dict[str, object],
        redirect: RedirectResponse,
        error: Exception | None = None,
    ) -> None:
        self._token = token
        self._redirect = redirect
        self._error = error
        self.redirect_calls: list[tuple[Request, str]] = []

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> RedirectResponse:
        self.redirect_calls.append((request, redirect_uri))
        return self._redirect

    async def authorize_access_token(self, request: Request) -> dict[str, object]:
        if self._error is not None:
            raise self._error
        return self._token


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "headers": []})


async def test_authorize_redirect_delegates_to_the_wrapped_app() -> None:
    redirect = RedirectResponse(url="https://accounts.google.com/o/oauth2/auth")
    app = StubOAuth2App(token={}, redirect=redirect)
    client = AuthlibGoogleOAuthClient(app)
    request = _request()

    result = await client.authorize_redirect(request, "http://localhost/auth/callback")

    assert result is redirect
    assert app.redirect_calls == [(request, "http://localhost/auth/callback")]


async def test_authorize_access_token_extracts_userinfo_into_google_claims() -> None:
    token: dict[str, object] = {
        "access_token": "should-never-be-read",
        "refresh_token": "should-never-be-read",
        "userinfo": {
            "sub": "google-sub-1",
            "email": "athlete@example.com",
            "email_verified": True,
            "name": "Athlete",
            "locale": "en",
        },
    }
    app = StubOAuth2App(token=token, redirect=RedirectResponse(url="/"))
    client = AuthlibGoogleOAuthClient(app)

    claims = await client.authorize_access_token(_request())

    assert claims.sub == "google-sub-1"
    assert claims.email == "athlete@example.com"
    assert claims.email_verified is True


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OAuthError(error="access_denied"), id="cancelled-at-google"),
        pytest.param(ConnectionError("google unreachable"), id="network-failure"),
        pytest.param(_IdTokenRejectedError("iss"), id="id-token-fails-validation"),
    ],
)
async def test_every_failed_token_exchange_surfaces_as_oauth_error(error: Exception) -> None:
    """The callback maps `OAuthError` to the sign-in page's inline alert (ticket #59), so a
    network error, a Google 5xx or a rejected ID token must not escape as anything else and
    land on the 500 page (skeptic finding, ticket #59 review gate)."""
    app = StubOAuth2App(token={}, redirect=RedirectResponse(url="/"), error=error)
    client = AuthlibGoogleOAuthClient(app)

    with pytest.raises(OAuthError):
        await client.authorize_access_token(_request())


@pytest.mark.parametrize(
    "token",
    [
        pytest.param({"access_token": "x"}, id="no-userinfo"),
        pytest.param({"userinfo": {"sub": "google-sub-1"}}, id="userinfo-missing-email"),
    ],
)
async def test_unusable_claims_surface_as_oauth_error(token: dict[str, object]) -> None:
    app = StubOAuth2App(token=token, redirect=RedirectResponse(url="/"))
    client = AuthlibGoogleOAuthClient(app)

    with pytest.raises(OAuthError):
        await client.authorize_access_token(_request())
