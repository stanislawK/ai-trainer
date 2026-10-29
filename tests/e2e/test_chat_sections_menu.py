import pytest
from playwright.sync_api import FloatRect, Locator, Page, expect

from tests.e2e.conftest import BASE_URL


def _box(locator: Locator) -> FloatRect:
    box = locator.bounding_box()
    assert box is not None
    return box


def _open_long_chat_at_bottom(page: Page) -> None:
    """A long chat, scrolled to its end. The page only has to be scrollable for the menu's
    positioning to matter, so a tall spacer stands in for hundreds of seeded messages."""
    page.goto(f"{BASE_URL}/")
    page.evaluate(
        """() => {
            const spacer = document.createElement("div");
            spacer.style.height = "4000px";
            document.querySelector("main").prepend(spacer);
            window.scrollTo(0, document.documentElement.scrollHeight);
        }"""
    )
    assert page.evaluate("window.scrollY") > 1000


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


def test_sections_menu_opens_in_view_at_the_bottom_of_a_long_chat(signed_in_page: Page) -> None:
    """Opened from the end of a long chat, the menu shows fully in the viewport, directly
    under the sticky header (#318: it used to open at the top of the document)."""
    _open_long_chat_at_bottom(signed_in_page)
    header = signed_in_page.locator("header:has([data-sections-trigger])")
    menu = signed_in_page.locator("#sections-menu")

    signed_in_page.locator("[data-sections-trigger]").click()
    expect(menu).to_be_visible()

    viewport = signed_in_page.viewport_size
    assert viewport is not None
    menu_box = _box(menu)
    header_box = _box(header)
    assert menu_box["y"] >= header_box["y"] + header_box["height"], "menu overlaps the header"
    assert menu_box["y"] - (header_box["y"] + header_box["height"]) <= 24, "menu not under header"
    assert menu_box["y"] + menu_box["height"] <= viewport["height"], "menu runs off the screen"


def test_sections_menu_stays_in_place_while_the_page_scrolls(signed_in_page: Page) -> None:
    """With the menu open, scrolling leaves it where it is, next to the sticky X (#318)."""
    _open_long_chat_at_bottom(signed_in_page)
    trigger = signed_in_page.locator("[data-sections-trigger]")
    menu = signed_in_page.locator("#sections-menu")
    trigger.click()
    expect(menu).to_be_visible()
    before = _box(menu)

    scrolled_from = signed_in_page.evaluate("window.scrollY")
    signed_in_page.evaluate("window.scrollBy(0, -1500)")
    assert signed_in_page.evaluate("window.scrollY") < scrolled_from - 1000

    assert _box(menu) == before
    expect(menu).to_be_in_viewport()
    expect(trigger).to_be_in_viewport()
    expect(trigger).to_have_attribute("aria-expanded", "true")
    assert abs(before["x"] - _box(trigger)["x"]) <= 1, "menu not aligned with the X"


@pytest.mark.parametrize("inset_top", [47, 120])
def test_sections_menu_clears_the_top_safe_area_and_the_header(
    signed_in_page: Page, inset_top: int
) -> None:
    """Under a notch the menu starts below the top safe-area inset and still sits directly
    under the header; past the header's own height it follows the inset (#318). Chromium
    fakes the inset through CDP, since a desktop browser has none."""
    cdp = signed_in_page.context.new_cdp_session(signed_in_page)
    cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": {"top": inset_top, "bottom": 34}})
    _open_long_chat_at_bottom(signed_in_page)
    header = signed_in_page.locator("header:has([data-sections-trigger])")
    menu = signed_in_page.locator("#sections-menu")
    signed_in_page.locator("[data-sections-trigger]").click()
    expect(menu).to_be_visible()

    menu_box = _box(menu)
    header_box = _box(header)
    assert menu_box["y"] >= inset_top, "menu under the notch"
    assert menu_box["y"] >= header_box["y"] + header_box["height"], "menu overlaps the header"
    assert menu_box["y"] <= max(64, inset_top + 8), "menu not directly under the header"
    assert menu_box["y"] + menu_box["height"] <= 844 - 34, "menu under the home indicator"


@pytest.mark.parametrize("how", ["escape", "tap outside", "x button"])
def test_sections_menu_closes_and_resets_aria_expanded(signed_in_page: Page, how: str) -> None:
    """Escape, light-dismiss and the X each close the popover (ADR-0019 wiring, #318)."""
    _open_long_chat_at_bottom(signed_in_page)
    trigger = signed_in_page.locator("[data-sections-trigger]")
    menu = signed_in_page.locator("#sections-menu")
    trigger.click()
    expect(trigger).to_have_attribute("aria-expanded", "true")

    if how == "escape":
        signed_in_page.keyboard.press("Escape")
    elif how == "tap outside":
        signed_in_page.mouse.click(300, 600)
    else:
        trigger.click()

    expect(menu).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")


def test_sections_menu_taller_than_the_viewport_scrolls_inside_itself(
    signed_in_page: Page,
) -> None:
    """On a short phone the menu is capped to the viewport and scrolls within itself rather
    than being clipped (#318 edge case)."""
    signed_in_page.set_viewport_size({"width": 390, "height": 150})
    _open_long_chat_at_bottom(signed_in_page)
    menu = signed_in_page.locator("#sections-menu")
    signed_in_page.locator("[data-sections-trigger]").click()
    expect(menu).to_be_visible()

    menu_box = _box(menu)
    assert menu_box["y"] + menu_box["height"] <= 150, "menu clipped by the viewport"
    assert menu.evaluate("el => el.scrollHeight > el.clientHeight"), "menu does not overflow"
    menu.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    expect(menu.locator("li").last).to_be_in_viewport()
