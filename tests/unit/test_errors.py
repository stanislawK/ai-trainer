"""Designed 404 and 500 pages, wired through FastAPI exception handlers (ADR-0019, ticket
#44). The 401 and 403 pages are covered where they're actually rendered: `test_active_user_gate.py`
and `test_web_csrf.py`."""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ai_trainer.web.errors import register_error_handlers
from ai_trainer.web.templating import build_templates


def _client() -> TestClient:
    app = FastAPI()
    templates = build_templates()
    register_error_handlers(app, templates)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("a very private database connection string")

    @app.get("/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="I'm a teapot")

    # `raise_server_exceptions=False`: `ServerErrorMiddleware` always re-raises after sending
    # the handler's response (Starlette's documented behavior, so servers can still log it),
    # so exercising the 500 path needs the client to not propagate that re-raise as a test error.
    return TestClient(app, raise_server_exceptions=False)


def test_unmatched_route_returns_the_designed_404_page_with_a_link_home() -> None:
    client = _client()

    response = client.get("/nowhere")

    assert response.status_code == 404
    assert "<html" in response.text
    assert "Off route" in response.text
    assert 'href="/"' in response.text


def test_unmatched_route_with_hx_request_returns_a_404_partial() -> None:
    client = _client()

    response = client.get("/nowhere", headers={"HX-Request": "true"})

    assert response.status_code == 404
    assert "<html" not in response.text
    assert "Off route" in response.text


def test_a_non_404_http_exception_still_gets_the_default_json_response() -> None:
    """Only 404 is restyled; every other `HTTPException` status keeps FastAPI's default
    behavior (ticket #44 scopes the redesign to 404/500/401/403, not every HTTP status)."""
    client = _client()

    response = client.get("/teapot")

    assert response.status_code == 418
    assert response.json() == {"detail": "I'm a teapot"}


def test_unhandled_exception_returns_the_designed_500_page() -> None:
    client = _client()

    response = client.get("/boom")

    assert response.status_code == 500
    assert "<html" in response.text
    assert "Something slipped" in response.text


def test_unhandled_exception_page_names_no_exception_detail() -> None:
    client = _client()

    response = client.get("/boom")

    assert "a very private database connection string" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_unhandled_exception_with_hx_request_returns_a_500_partial() -> None:
    client = _client()

    response = client.get("/boom", headers={"HX-Request": "true"})

    assert response.status_code == 500
    assert "<html" not in response.text
    assert "Something slipped" in response.text
