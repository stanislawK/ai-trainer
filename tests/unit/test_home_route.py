from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from ai_trainer.web.home import build_home_router
from ai_trainer.web.templating import STATIC_DIR, build_templates


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(build_home_router(build_templates()))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return TestClient(app)


def test_get_root_returns_full_page_with_stylesheet_link() -> None:
    client = _client()

    response = client.get("/")

    assert response.status_code == 200
    assert "<html" in response.text
    assert '<link rel="stylesheet" href="/static/css/app.css">' in response.text


def test_stylesheet_asset_is_servable_when_compiled() -> None:
    """app.css is a gitignored build artifact (`make css` / the Docker css-builder
    stage, ADR-0012) — a fresh checkout has no network access in pytest (ADR-0013),
    so this skips rather than fails when nobody has compiled it yet; it runs for
    real once `make css` (or the Dockerfile) has produced the file."""
    if not (STATIC_DIR / "css" / "app.css").is_file():
        pytest.skip("static/css/app.css not built — run `make css` first")

    client = _client()

    response = client.get("/static/css/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_get_root_with_hx_request_returns_partial_without_html_element() -> None:
    client = _client()

    response = client.get("/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert "<!doctype" not in response.text.lower()


def test_get_root_references_local_htmx_script_with_no_external_origin() -> None:
    client = _client()

    response = client.get("/")

    assert '<script src="/static/vendor/htmx-4.0.0.min.js">' in response.text
    assert "http://" not in response.text
    assert "https://" not in response.text


def test_vendored_htmx_file_exists_and_is_served() -> None:
    client = _client()

    assert (STATIC_DIR / "vendor" / "htmx-4.0.0.min.js").is_file()

    response = client.get("/static/vendor/htmx-4.0.0.min.js")

    assert response.status_code == 200


def test_htmx_vendor_file_is_not_an_empty_placeholder() -> None:
    contents = (STATIC_DIR / "vendor" / "htmx-4.0.0.min.js").read_text()

    assert len(contents) > 1000
    assert "htmx" in contents.lower()


def test_web_dir_has_the_required_template_and_static_layout() -> None:
    web_dir = Path(STATIC_DIR).parent

    assert (web_dir / "templates" / "pages").is_dir()
    assert (web_dir / "templates" / "partials").is_dir()
    assert (web_dir / "templates" / "components").is_dir()
    assert (web_dir / "static").is_dir()
