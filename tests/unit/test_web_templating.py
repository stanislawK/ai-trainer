"""The nav context processor (ADR-0019, ticket #40): every template gets the visible section
list, the current path and the request's user with no route having to thread it through by
hand — the same pattern `_csrf_context_processor` already uses."""

import jinja2
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.templating import Jinja2Templates

from ai_trainer.web.templating import _nav_context_processor

_PROBE_TEMPLATE = (
    "{{ nav_sections | map(attribute='id') | join(',') }}|"
    "{{ is_admin }}|"
    "{{ current_user }}|"
    "{{ current_path }}"
)


def _client() -> TestClient:
    app = FastAPI()
    env = jinja2.Environment(loader=jinja2.DictLoader({"probe.html": _PROBE_TEMPLATE}))
    templates = Jinja2Templates(env=env, context_processors=[_nav_context_processor])

    @app.get("/probe")
    async def probe(request: Request) -> object:
        return templates.TemplateResponse(request, "probe.html", {})

    @app.get("/probe-as-admin")
    async def probe_as_admin(request: Request) -> object:
        request.state.user = "admin@example.com"
        request.state.is_admin = True
        return templates.TemplateResponse(request, "probe.html", {})

    return TestClient(app)


def test_a_request_with_no_gate_middleware_gets_safe_defaults() -> None:
    client = _client()

    response = client.get("/probe")

    assert response.text == "chat,settings|False|None|/probe"


def test_an_admin_request_sees_every_section_and_its_own_identity() -> None:
    client = _client()

    response = client.get("/probe-as-admin")

    assert response.text == "chat,settings,admin|True|admin@example.com|/probe-as-admin"
