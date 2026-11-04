from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from ai_trainer.application.csrf import generate_csrf_token
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME, CsrfMiddleware
from ai_trainer.web.templating import build_templates

SECRET = b"unit-test-secret"


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/state-probe")
    async def state_probe(request: Request) -> PlainTextResponse:
        token = getattr(request.state, "csrf_token", None)
        return PlainTextResponse(token or "")

    @app.post("/action")
    async def action() -> PlainTextResponse:
        return PlainTextResponse("done")

    @app.post("/api/admin/knowledge/sources")
    async def api_action() -> PlainTextResponse:
        return PlainTextResponse("api done")

    @app.post("/api/admin/knowledgex")
    async def look_alike() -> PlainTextResponse:
        return PlainTextResponse("look-alike")

    app.add_middleware(CsrfMiddleware, secret_key=SECRET, templates=build_templates())
    return TestClient(app)


def test_get_request_exposes_the_csrf_token_bound_to_the_session_cookie() -> None:
    client = _client()
    session_id = uuid4()
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))

    response = client.get("/state-probe")

    assert response.text == generate_csrf_token(session_id, SECRET)


def test_get_request_with_no_session_cookie_exposes_no_csrf_token() -> None:
    client = _client()

    response = client.get("/state-probe")

    assert response.text == ""


def test_get_request_is_never_blocked_even_with_no_token() -> None:
    client = _client()

    response = client.get("/state-probe")

    assert response.status_code == 200


def test_post_with_the_matching_token_succeeds() -> None:
    client = _client()
    session_id = uuid4()
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    token = generate_csrf_token(session_id, SECRET)

    response = client.post("/action", headers={CSRF_HEADER_NAME: token})

    assert response.status_code == 200
    assert response.text == "done"


def test_post_with_no_token_is_rejected() -> None:
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post("/action")

    assert response.status_code == 403


def test_rejection_renders_the_full_error_page_for_a_plain_request() -> None:
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post("/action")

    assert response.status_code == 403
    assert "<html" in response.text
    assert "CSRF token missing or invalid" not in response.text


def test_rejection_page_offers_a_reload_action() -> None:
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post("/action")

    assert "data-reload-page" in response.text


def test_rejection_renders_an_htmx_partial_for_an_hx_request() -> None:
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post("/action", headers={"HX-Request": "true"})

    assert response.status_code == 403
    assert "<html" not in response.text


def test_post_with_no_session_cookie_is_rejected_even_with_a_token() -> None:
    client = _client()

    response = client.post("/action", headers={CSRF_HEADER_NAME: "anything"})

    assert response.status_code == 403


def test_post_with_another_sessions_token_is_rejected() -> None:
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))
    foreign_token = generate_csrf_token(uuid4(), SECRET)

    response = client.post("/action", headers={CSRF_HEADER_NAME: foreign_token})

    assert response.status_code == 403


def test_post_with_a_malformed_session_cookie_is_rejected() -> None:
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-uuid")

    response = client.post("/action", headers={CSRF_HEADER_NAME: "anything"})

    assert response.status_code == 403


def test_the_token_routes_are_not_csrf_checked_and_a_look_alike_is() -> None:
    """ADR-0005 invariant 3 as amended by ADR-0036: a bearer header is never sent by a browser
    on its own, so the token routes take no CSRF token; they take no cookie either."""
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    assert client.post("/api/admin/knowledge/sources").text == "api done"
    assert client.post("/api/admin/knowledgex").status_code == 403
