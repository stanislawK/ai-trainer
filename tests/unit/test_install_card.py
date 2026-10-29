"""Add to home screen (PRD 0009 F22, ticket #307, the "Add to home screen" mock): a phone-only
card in the chat and an always-present section in Settings. The server renders every variant
hidden; `static/js/install.js` picks the one that fits the browser, keeps the
`beforeinstallprompt` event for Install, and stores a dismissal or install in
`localStorage["install-card"]`, like the theme. No service worker (ADR-0019)."""

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.web.templating import STATIC_DIR, build_templates
from tests.chat_support import include_chat, sign_in_as

_TEMPLATES = Path(__file__).parents[2] / "src" / "ai_trainer" / "web" / "templates"
_INSTALL_JS = STATIC_DIR / "js" / "install.js"


def _render(template: str, **context: object) -> str:
    return build_templates().env.get_template(template).render(**context)


def _chat_page() -> str:
    app = FastAPI()
    include_chat(app, build_templates())
    sign_in_as(app)
    return TestClient(app).get("/").text


def test_chat_page_carries_the_install_card_hidden_until_the_script_shows_it() -> None:
    html = _chat_page()

    card = html.partition("data-install-card")[2].partition(">")[0]
    assert "hidden" in card
    assert "Add AI Trainer to your home screen" in html


def test_card_sits_above_the_composer_outside_it() -> None:
    """The composer swaps its own outerHTML on every send, so the card must live beside it."""
    html = _chat_page()

    assert html.index("data-install-card") < html.index('id="chat-composer"')


def test_card_offers_install_on_android_and_share_steps_on_ios() -> None:
    html = _render("partials/chat/install_card.html")

    for variant in ("prompt", "ios-safari", "ios-chrome"):
        assert f'data-install-variant="{variant}"' in html
    assert "data-install-action" in html
    assert "Install" in html
    assert "in the toolbar below" in html
    assert "in the address bar above" in html
    assert "Add to Home Screen" in html


def test_card_has_a_dismiss_control_with_a_name() -> None:
    html = _render("partials/chat/install_card.html")

    [dismiss] = re.findall(r"<button[^>]*data-install-dismiss[^>]*>", html)
    assert 'aria-label="Dismiss"' in dismiss


def test_only_the_chat_includes_the_card() -> None:
    """AC: no onboarding, sign-in, status or error page renders the card — nor any layout."""
    including = sorted(
        str(path.relative_to(_TEMPLATES))
        for path in _TEMPLATES.rglob("*.html")
        if "install_card.html" in path.read_text() or "data-install-card" in path.read_text()
    )

    assert including == ["partials/chat/index.html", "partials/chat/install_card.html"]


def test_settings_offers_add_to_home_screen_between_appearance_and_danger_zone() -> None:
    html = _render("partials/settings/index.html")

    appearance = html.index('aria-label="Appearance"')
    install = html.index('aria-label="Add to home screen"')
    danger = html.index('aria-label="Danger zone"')
    assert appearance < install < danger


def _settings_variants() -> dict[tuple[str, str | None], str]:
    """Each Settings variant's text, keyed by its state and the platforms it is for."""
    html = _render("partials/settings/index.html")
    section = html.partition("data-install-settings")[2].partition("</section>")[0]
    variants: dict[tuple[str, str | None], str] = {}
    for match in re.finditer(r'<div [^>]*data-install-state="([^"]+)"([^>]*)>', section):
        tag = match.group(0)
        assert " hidden" in tag, tag
        on = re.search(r'data-install-on="([^"]+)"', tag)
        body = section[match.end() :].partition('data-install-state="')[0]
        variants[(match.group(1), on.group(1) if on else None)] = re.sub(r"<[^>]+>", "", body)
    return variants


def test_settings_section_and_every_state_start_hidden() -> None:
    html = _render("partials/settings/index.html")

    section = re.search(r"<section [^>]*data-install-settings[^>]*>", html)
    assert section is not None
    assert " hidden" in section.group(0)
    assert set(_settings_variants()) == {
        ("prompt", "android ios"),
        ("prompt", "desktop"),
        ("ios-safari", None),
        ("ios-chrome", None),
        ("installed", "android ios"),
        ("installed", "desktop"),
        ("unsupported", "android"),
        ("unsupported", "ios"),
        ("unsupported", "desktop"),
    }


def test_settings_install_states_carry_the_install_button() -> None:
    html = _render("partials/settings/index.html")

    assert html.count("data-install-action") == 2


def test_settings_says_installed_on_phone_and_desktop() -> None:
    variants = _settings_variants()

    assert "from your home screen" in variants[("installed", "android ios")]
    assert "as an installed app" in variants[("installed", "desktop")]


def test_settings_steps_point_at_share_where_each_ios_browser_has_it() -> None:
    variants = _settings_variants()

    assert "In Safari it takes two taps" in variants[("ios-safari", None)]
    assert "toolbar below" in variants[("ios-safari", None)]
    assert "In Chrome it takes two taps" in variants[("ios-chrome", None)]
    assert "address bar above" in variants[("ios-chrome", None)]


def test_settings_names_a_browser_that_can_install_per_platform() -> None:
    """AC: a browser that can't install names one that can (Chrome on Android, Safari on an
    iPhone, Chrome or Edge on desktop Firefox)."""
    variants = _settings_variants()

    assert "Open AI Trainer in Chrome to install it" in variants[("unsupported", "android")]
    assert "Open AI Trainer in Safari to install it" in variants[("unsupported", "ios")]
    assert "Firefox can't install apps" in variants[("unsupported", "desktop")]
    assert "in Chrome or Edge to install it" in variants[("unsupported", "desktop")]


def test_every_page_loads_the_install_script_after_theme() -> None:
    html = _chat_page()

    assert '<script src="/static/js/install.js" defer></script>' in html
    assert html.index("/static/js/theme.js") < html.index("/static/js/install.js")


def test_install_script_keeps_the_prompt_and_stores_like_the_theme() -> None:
    script = _INSTALL_JS.read_text()

    assert "beforeinstallprompt" in script
    assert "preventDefault" in script
    assert ".prompt()" in script
    assert "appinstalled" in script
    assert "display-mode: standalone" in script
    assert "CriOS" in script
    assert "localStorage.setItem(STORAGE_KEY" in script
    assert "catch" in script
    assert "htmx:after:swap" in script
    assert "serviceWorker" not in script


def test_head_keeps_an_install_offer_that_fires_before_the_deferred_script() -> None:
    head = _chat_page().partition("</head>")[0]
    inline = head.partition("<script>")[2].partition("</script>")[0]

    assert 'addEventListener("beforeinstallprompt"' in inline
    assert "preventDefault" in inline
    assert "window.__installPrompt" in _INSTALL_JS.read_text()
