"""Theme persistence (ADR-0019, ticket #38): the stored choice, read in `layouts/base.html`'s
inline `<head>` script, decides `data-theme` before first paint — never the app default flashing
first and never a storage failure leaking to the console."""

from playwright.sync_api import ConsoleMessage, Page

from tests.e2e.conftest import BASE_URL


def test_first_visit_renders_trainer_dark(signed_in_page: Page) -> None:
    signed_in_page.goto(f"{BASE_URL}/")

    theme = signed_in_page.evaluate("document.documentElement.dataset.theme")

    assert theme == "trainer-dark"


def test_stored_light_theme_persists_and_is_already_set_at_domcontentloaded(
    signed_in_page: Page,
) -> None:
    """Seeds `localStorage["theme"]` before any page script runs, then reads `data-theme`
    right at `domcontentloaded`. This confirms the stored choice survives a reload and that
    `data-theme` is already correct by the time the synchronous parser reaches <body> — the
    inline <head> script runs before body content exists to paint, which is what rules out a
    dark flash (a later, async check couldn't tell a synchronous-but-late script from an
    early one; Playwright has no screencast/trace here to observe intermediate paints
    directly, so this asserts the mechanism — <head>-script-before-<body> — rather than
    the absence of a rendered frame)."""
    signed_in_page.add_init_script("window.localStorage.setItem('theme', 'trainer-light')")

    signed_in_page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")
    theme = signed_in_page.evaluate("document.documentElement.dataset.theme")

    assert theme == "trainer-light"


def test_storage_that_throws_still_renders_dark_with_no_console_error(
    signed_in_page: Page,
) -> None:
    signed_in_page.add_init_script(
        "Object.defineProperty(window, 'localStorage', {"
        " get() { throw new DOMException('blocked'); } })"
    )
    console_errors: list[str] = []

    def _on_console(msg: ConsoleMessage) -> None:
        if msg.type == "error":
            console_errors.append(msg.text)

    signed_in_page.on("console", _on_console)
    signed_in_page.on("pageerror", lambda exc: console_errors.append(str(exc)))

    signed_in_page.goto(f"{BASE_URL}/")
    theme = signed_in_page.evaluate("document.documentElement.dataset.theme")

    assert theme == "trainer-dark"
    assert console_errors == []
