from playwright.sync_api import Page

from tests.e2e.conftest import BASE_URL


def test_home_page_has_no_horizontal_overflow_at_390px(signed_in_page: Page) -> None:
    """`browser_context_args` in conftest.py sets the 390x844 viewport (PRD-0003 G11: every
    page works at 390px wide with no horizontal scroll)."""
    signed_in_page.goto(f"{BASE_URL}/")

    scroll_width = signed_in_page.evaluate("document.documentElement.scrollWidth")
    client_width = signed_in_page.evaluate("document.documentElement.clientWidth")

    assert scroll_width <= client_width, (
        f"page scrolls horizontally at 390px: scrollWidth={scroll_width} "
        f"> clientWidth={client_width}"
    )
