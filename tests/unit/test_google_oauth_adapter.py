from fastapi import Request
from fastapi.responses import RedirectResponse

from ai_trainer.adapters.google_oauth import AuthlibGoogleOAuthClient


class StubOAuth2App:
    def __init__(self, token: dict[str, object], redirect: RedirectResponse) -> None:
        self._token = token
        self._redirect = redirect
        self.redirect_calls: list[tuple[Request, str]] = []

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> RedirectResponse:
        self.redirect_calls.append((request, redirect_uri))
        return self._redirect

    async def authorize_access_token(self, request: Request) -> dict[str, object]:
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
