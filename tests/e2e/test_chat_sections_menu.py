from playwright.sync_api import Page, expect

from tests.e2e.conftest import BASE_URL


def test_sections_menu_opens_and_closes_by_keyboard(signed_in_page: Page) -> None:
    """The chat page has no dock (ADR-0019 pattern 2c): the sections menu button is the only
    way to navigate away from it on mobile, so its keyboard behavior is a must-not-regress
    flow (skeptic finding, ticket #40 review gate — the native Popover API wiring was only
    verified ad hoc, never by a committed spec)."""
    signed_in_page.goto(f"{BASE_URL}/")
    trigger = signed_in_page.locator("[data-sections-trigger]")
    menu = signed_in_page.locator("#sections-menu")

    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(menu).to_be_hidden()

    trigger.focus()
    signed_in_page.keyboard.press("Enter")

    expect(trigger).to_have_attribute("aria-expanded", "true")
    expect(menu).to_be_visible()

    signed_in_page.keyboard.press("Escape")

    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(menu).to_be_hidden()
