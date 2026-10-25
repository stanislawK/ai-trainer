"""Per-address cap on the sign-in routes, and a per-session cap on everything else (ADR-0029).

Pure ASGI, not `BaseHTTPMiddleware`, so a reply stream is not buffered. The sign-in routes
are keyed by Cloudflare's `CF-Connecting-IP` (the origin only accepts Cloudflare), and by
the TCP peer when that header is absent. A session cookie keys the rest. `/health` and
`/static` are not counted.
"""

from datetime import timedelta
from typing import cast

from starlette.requests import Request
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.sliding_window import SlidingWindow
from ai_trainer.web.auth import SESSION_COOKIE_NAME

_MINUTE = timedelta(minutes=1)
_AUTH_PATHS = frozenset({"/auth/login", "/auth/callback", "/sign-in"})
_EXEMPT_PATHS = frozenset({"/health"})
_STATIC_PREFIX = "/static/"
_CF_CONNECTING_IP = b"cf-connecting-ip"
_COOKIE = b"cookie"
_MAX_ADDRESS_LENGTH = 64
_MAX_COOKIE_LENGTH = 200
_TOO_MANY = 429


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        clock: ClockPort,
        templates: Jinja2Templates,
        auth_per_minute: int,
        session_per_minute: int,
    ) -> None:
        self._app = app
        self._templates = templates
        self._auth_per_minute = auth_per_minute
        self._session_per_minute = session_per_minute
        self._window = SlidingWindow(clock, window=_MINUTE)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        retry_after = self._retry_after(scope)
        if retry_after is None:
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive)
        is_htmx = request.headers.get("HX-Request") == "true"
        template = "partials/errors/429.html" if is_htmx else "pages/errors/429.html"
        response = self._templates.TemplateResponse(request, template, status_code=_TOO_MANY)
        response.headers["Retry-After"] = str(retry_after)
        await response(scope, receive, send)

    def _retry_after(self, scope: Scope) -> int | None:
        path = scope["path"]
        canonical = path.rstrip("/") or "/"
        if canonical in _EXEMPT_PATHS or path.startswith(_STATIC_PREFIX):
            return None
        if canonical in _AUTH_PATHS:
            return self._window.allow(
                f"auth:{_client_address(scope)}",
                limit=self._auth_per_minute,
            )
        session = _session_cookie(scope)
        if session is None:
            return None
        return self._window.allow(f"session:{session}", limit=self._session_per_minute)


def _client_address(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name.lower() != _CF_CONNECTING_IP:
            continue
        text = cast(str, value.decode("latin-1")).strip()
        if text and len(text) <= _MAX_ADDRESS_LENGTH and "\n" not in text and "\r" not in text:
            return text
        break
    client = scope.get("client")
    if isinstance(client, tuple) and client and isinstance(client[0], str) and client[0]:
        return client[0]
    return "unknown"


def _session_cookie(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() != _COOKIE:
            continue
        for part in cast(str, value.decode("latin-1")).split(";"):
            key, separator, raw = part.strip().partition("=")
            if separator and key == SESSION_COOKIE_NAME and raw:
                return raw[:_MAX_COOKIE_LENGTH]
    return None
