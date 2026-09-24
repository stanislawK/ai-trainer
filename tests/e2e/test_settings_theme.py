"""Theme persistence on the Settings page (ADR-0019, ticket #41), explicitly named as
e2e-worthy in `tests.md`. The toggle is pure client-side JS: no server round trip, and it must
keep working even when `localStorage` is unavailable."""

from playwright.sync_api import Page, expect

from tests.e2e.conftest import BASE_URL


def test_switching_to_light_applies_immediately_and_survives_reload(signed_in_page: Page) -> None:
    signed_in_page.goto(f"{BASE_URL}/settings")
    dark_button = signed_in_page.get_by_role("button", name="Dark")
    light_button = signed_in_page.get_by_role("button", name="Light")
    expect(dark_button).to_have_attribute("aria-pressed", "true")

    light_button.click()

    expect(light_button).to_have_attribute("aria-pressed", "true")
    expect(dark_button).to_have_attribute("aria-pressed", "false")
    expect(signed_in_page.locator("html")).to_have_attribute("data-theme", "trainer-light")

    signed_in_page.reload()

    expect(signed_in_page.locator("html")).to_have_attribute("data-theme", "trainer-light")
    expect(signed_in_page.get_by_role("button", name="Light")).to_have_attribute(
        "aria-pressed", "true"
    )


def test_switching_back_to_dark_restores_the_default_theme_and_storage(
    signed_in_page: Page,
) -> None:
    signed_in_page.goto(f"{BASE_URL}/settings")
    signed_in_page.get_by_role("button", name="Light").click()

    signed_in_page.get_by_role("button", name="Dark").click()

    expect(signed_in_page.locator("html")).to_have_attribute("data-theme", "trainer-dark")
    stored = signed_in_page.evaluate("() => localStorage.getItem('theme')")
    assert stored == "trainer-dark"


def test_theme_toggle_makes_no_request_to_the_server(signed_in_page: Page) -> None:
    signed_in_page.goto(f"{BASE_URL}/settings")
    requests: list[str] = []
    signed_in_page.on("request", lambda request: requests.append(request.url))

    signed_in_page.get_by_role("button", name="Light").click()

    assert requests == []


def test_theme_toggle_still_works_when_local_storage_is_unavailable(
    signed_in_page: Page,
) -> None:
    signed_in_page.context.add_init_script(
        "Object.defineProperty(window, 'localStorage', "
        "{ get() { throw new Error('storage disabled'); } });"
    )
    errors: list[str] = []
    signed_in_page.on("pageerror", lambda exc: errors.append(str(exc)))

    signed_in_page.goto(f"{BASE_URL}/settings")
    signed_in_page.get_by_role("button", name="Light").click()

    expect(signed_in_page.locator("html")).to_have_attribute("data-theme", "trainer-light")
    assert errors == []
