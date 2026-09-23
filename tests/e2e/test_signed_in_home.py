from playwright.sync_api import Page, expect

from tests.e2e.conftest import BASE_URL


def test_reaches_signed_in_home_page_using_dev_session_cookie(signed_in_page: Page) -> None:
    """The only way this page is reached signed-in: `dev_session.py`'s cookie, never a real
    Google sign-in (ADR-0013)."""
    response = signed_in_page.goto(f"{BASE_URL}/")

    assert response is not None
    assert response.status == 200
    expect(signed_in_page.locator("#home-content")).to_be_visible()
    expect(signed_in_page.locator("h1")).to_have_text("AI Trainer")
