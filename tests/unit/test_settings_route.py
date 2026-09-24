"""Wires `GET /settings` (ADR-0019, ticket #41): the Appearance section only. The theme
choice is applied and stored entirely client-side by `static/js/theme.js`; the route itself
carries no theme state (nothing is stored server-side)."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from ai_trainer.web.settings import build_settings_router
from ai_trainer.web.templating import STATIC_DIR, build_templates


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(build_settings_router(build_templates()))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return TestClient(app)


def test_get_settings_returns_full_page_with_appearance_section() -> None:
    client = _client()

    response = client.get("/settings")

    assert response.status_code == 200
    assert "<html" in response.text
    assert '<h2 class="text-[13px] font-medium text-base-content/70 px-1">Appearance</h2>' in (
        response.text
    )


def test_get_settings_with_hx_request_returns_partial_without_html_element() -> None:
    client = _client()

    response = client.get("/settings", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert "<!doctype" not in response.text.lower()


def test_theme_toggle_renders_dark_and_light_buttons_with_aria_pressed() -> None:
    client = _client()

    response = client.get("/settings")

    assert 'data-theme-option="trainer-dark"' in response.text
    assert 'data-theme-option="trainer-light"' in response.text
    assert response.text.count('aria-pressed="true"') == 1
    assert response.text.count('aria-pressed="false"') == 1


def test_theme_toggle_defaults_to_dark_pressed() -> None:
    """The server always renders the `trainer-dark` default (ADR-0019): the actual stored
    choice is only known in the browser, and the pre-paint script in `base.html` plus
    `theme.js` correct the control's visual/aria state on load when it differs."""
    client = _client()

    response = client.get("/settings")

    dark_start = response.text.index('data-theme-option="trainer-dark"')
    dark_tag_start = response.text.rindex("<button", 0, dark_start)
    dark_tag_end = response.text.index(">", dark_start)
    dark_button = response.text[dark_tag_start:dark_tag_end]

    assert 'aria-pressed="true"' in dark_button


def test_theme_toggle_buttons_carry_no_htmx_attributes() -> None:
    """The choice makes no request to the server (AC): the toggle is pure client-side JS,
    never an htmx `hx-post`/`hx-get`."""
    client = _client()

    response = client.get("/settings")

    section_start = response.text.index('aria-label="Appearance"')
    section_end = response.text.index("</section>", section_start)
    section_html = response.text[section_start:section_end]

    assert "hx-post" not in section_html
    assert "hx-get" not in section_html


def test_get_settings_references_theme_script() -> None:
    client = _client()

    response = client.get("/settings")

    assert '<script src="/static/js/theme.js" defer></script>' in response.text
