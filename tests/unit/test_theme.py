"""Static checks for the Liquid Glass theme wiring (ADR-0019, ticket #38): the default theme,
the before-paint script, the refraction filter, and the build machinery that keeps `make css`
and the Docker build in sync."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.web.home import build_home_router
from ai_trainer.web.templating import STATIC_DIR, build_templates

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(build_home_router(build_templates()))
    return TestClient(app)


def test_base_layout_defaults_to_trainer_dark_theme() -> None:
    response = _client().get("/")

    assert 'data-theme="trainer-dark"' in response.text


def test_base_layout_carries_an_inline_before_paint_theme_script() -> None:
    """The script must run synchronously in <head>, before <body>, and read
    `localStorage["theme"]` inside a try/catch so a throw still leaves `trainer-dark`."""
    response = _client().get("/")
    head, _, body = response.text.partition("<body")

    assert "<script>" in head
    assert 'localStorage.getItem("theme")' in head or "localStorage['theme']" in head
    assert "trainer-dark" in head
    assert "catch" in head
    assert body


def test_base_layout_carries_the_lg_refract_svg_filter() -> None:
    response = _client().get("/")

    assert 'id="lg-refract"' in response.text
    assert "feDisplacementMap" in response.text


def test_base_layout_body_has_the_wallpaper_class() -> None:
    response = _client().get("/")

    assert "wallpaper" in response.text


def test_glass_css_defines_the_closed_set_of_utilities() -> None:
    glass_css = (STATIC_DIR / "css" / "glass.css").read_text()

    for utility in ("wallpaper", "glass-clear", "glass-regular", "glass-thick", "glass-rim"):
        assert f"@utility {utility}" in glass_css


def _utility_block(css: str, name: str) -> str:
    """The text of one `@utility <name> { ... }` block, brace-matched so a fallback rule
    found here is provably nested inside *this* utility and not merely present somewhere
    else in the file (AC4: skeptic review, ticket #38 — a plain substring search over the
    whole file can't tell the two apart)."""
    start = css.index(f"@utility {name} {{") + len(f"@utility {name} {{")
    depth = 1
    for index, char in enumerate(css[start:], start=start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return css[start:index]
    raise AssertionError(f"unterminated @utility {name} block in glass.css")


def test_glass_utilities_fall_back_to_no_backdrop_filter_under_reduced_contrast() -> None:
    """Every surface with a `backdrop-filter` gets an opaque fallback under
    `prefers-reduced-transparency: reduce` or `prefers-contrast: more` (ADR-0019 invariant 4).
    Checked per utility block, not just anywhere in the file, so a fallback accidentally
    misplaced under the wrong utility (or dropped from one of them) fails this test.

    This proves the *source* compiles correctly (verified by hand: forcing Tailwind to emit
    these classes via a temporary `@source inline(...)` and probing `getComputedStyle` in a
    real Chromium tab under `prefers-contrast: more` showed `backdrop-filter: none` for
    glass-clear/regular/thick). It does not prove today's *shipped* `app.css` contains any
    glass rule at all: Tailwind v4's JIT only emits a utility once some template references
    its class, and no template does yet — `wallpaper` is the only one of this set actually
    used, by `layouts/base.html`. The other four become real, compiled CSS starting with the
    ticket that first renders a dock, composer or card (#40+)."""
    glass_css = (STATIC_DIR / "css" / "glass.css").read_text()

    for utility in ("glass-clear", "glass-regular", "glass-thick"):
        block = _utility_block(glass_css, utility)
        assert "backdrop-filter: blur(" in block
        assert "prefers-reduced-transparency: reduce" in block
        assert "prefers-contrast: more" in block
        assert "backdrop-filter: none" in block

    rim_block = _utility_block(glass_css, "glass-rim")
    assert "prefers-reduced-transparency: reduce" in rim_block
    assert "prefers-contrast: more" in rim_block
    assert "display: none" in rim_block

    wallpaper_block = _utility_block(glass_css, "wallpaper")
    assert "prefers-reduced-transparency: reduce" in wallpaper_block
    assert "prefers-contrast: more" in wallpaper_block
    assert "background-image: none" in wallpaper_block


def test_theme_css_defines_both_themes_with_dark_as_default_and_prefersdark() -> None:
    theme_css = (STATIC_DIR / "css" / "theme.css").read_text()

    assert 'name: "trainer-dark"' in theme_css
    assert 'name: "trainer-light"' in theme_css
    assert "default: true" in theme_css
    assert "prefersdark: true" in theme_css


def test_input_css_disables_built_in_themes_and_imports_theme_and_glass() -> None:
    input_css = (STATIC_DIR / "css" / "input.css").read_text()

    assert "themes: false" in input_css
    assert '@import "./theme.css"' in input_css
    assert '@import "./glass.css"' in input_css


def test_build_css_script_downloads_the_daisyui_theme_bundle_next_to_daisyui() -> None:
    script = (REPO_ROOT / "scripts" / "build_css.sh").read_text()

    assert "daisyui-theme.js" in script
    assert "daisyui.js" in script


def test_dockerfile_css_builder_stage_copies_theme_and_glass_css() -> None:
    """The Docker build must see the same theme.css/glass.css `make css` compiles locally
    (AC5), or the two builds would emit different CSS."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    css_builder_stage = dockerfile.split("AS css-builder", 1)[1].split("FROM", 1)[0]

    assert "static/css/theme.css" in css_builder_stage
    assert "static/css/glass.css" in css_builder_stage


def test_daisyui_theme_bundle_is_gitignored_like_daisyui_js() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text()

    assert "daisyui-theme.js" in gitignore
