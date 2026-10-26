"""Each request's `request_id` and access line (ADR-0030)."""

import logging
import re
import time
from uuid import uuid4

from opentelemetry.instrumentation.fastapi import _get_route_details
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware.errors import ServerErrorMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_trainer.adapters.log_records import bind_request, unbind_request
from ai_trainer.application.ports.metrics import NO_METRICS, MetricsPort

REQUEST_ID_HEADER = "X-Request-ID"
# An incoming ID is kept only if it can't break a log line or a header; otherwise a new one.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
# The container probe calls it every few seconds (ADR-0023).
_UNLOGGED_PATHS = frozenset({"/health", "/health/"})
# A metric label is a bounded set (ADR-0030 invariant 4): any other method is `_OTHER`, as
# OpenTelemetry's HTTP conventions name it, and a path no route matches is `unmatched`.
_KNOWN_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)
_UNMATCHED_ROUTE = "unmatched"

access_logger = logging.getLogger("ai_trainer.access")


def log_every_request(app: Starlette, metrics: MetricsPort = NO_METRICS) -> None:
    """Puts `RequestLogMiddleware` outside the stack Starlette builds, its error middleware
    included, so the 500 that middleware sends carries the `X-Request-ID` and is logged.

    The stack stays wrapped in a plain `ServerErrorMiddleware` because OpenTelemetry's FastAPI
    instrumentor expects one outermost: it unwraps it and puts its span around what's inside,
    this middleware too.
    """
    build_stack = app.build_middleware_stack

    def build_logged_stack() -> ASGIApp:
        return ServerErrorMiddleware(RequestLogMiddleware(build_stack(), metrics))

    app.build_middleware_stack = build_logged_stack  # type: ignore[method-assign]


class RequestLogMiddleware:
    """Sets the request's `request_id`, returns it as `X-Request-ID` and writes the access
    line: method, route template, status and duration, never the raw path, its query or the
    client's address (ADR-0030 invariant 3). The same four make the request's metric."""

    def __init__(self, app: ASGIApp, metrics: MetricsPort = NO_METRICS) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid4().hex
        status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        token = bind_request(request_id)
        started = time.perf_counter()
        try:
            await self._app(scope, receive, send_with_request_id)
        except BaseException:
            # Left bound: the server logs the error after this returns, in the request's own
            # task, and that line belongs to this request too.
            self._log(scope, status, started)
            raise
        self._log(scope, status, started)
        unbind_request(token)

    def _log(self, scope: Scope, status: int, started: float) -> None:
        if scope["path"] in _UNLOGGED_PATHS:
            return
        elapsed = time.perf_counter() - started
        route = _log_access(scope, status, elapsed)
        method = scope["method"] if scope["method"] in _KNOWN_METHODS else "_OTHER"
        self._metrics.request(
            method=method, route=route or _UNMATCHED_ROUTE, status=status, seconds=elapsed
        )


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            incoming = value.decode("latin-1")
            return incoming if _VALID_REQUEST_ID.fullmatch(incoming) else None
    return None


def _log_access(scope: Scope, status: int, elapsed: float) -> str | None:
    # The same matching as the trace's `http.route`, so a log line and its span name one route.
    # It runs here because a request the gate or the host check refuses never reaches the
    # router, and it walks FastAPI's included routers, which have no `path` of their own.
    route = _get_route_details(scope)
    duration_ms = round(elapsed * 1000, 1)
    access_logger.info(
        "%s %s %d %.1fms",
        scope["method"],
        route,
        status,
        duration_ms,
        extra={
            "method": scope["method"],
            "route": route,
            "status": status,
            "duration_ms": duration_ms,
        },
    )
    return route
