"""A new account walks through onboarding into the chat (PRD-0007 F1, F6, G11; tickets #86, #254).

It is the first flow every athlete takes, so it earns a permanent spec (ADR-0013). The
streamed reply is out of scope: stubbing OpenRouter inside the compose stack is not decided
in any ADR, so the spec only asserts on the athlete's own message.
"""

from playwright.sync_api import Page, expect

from tests.e2e.conftest import BASE_URL

MESSAGE = "Climbed 6b+ boulders for an hour"


def _press(page: Page, name: str) -> None:
    """Clicks a step's button once htmx has wired the page. A click that lands before
    DOMContentLoaded would submit the form natively (a GET with the fields in the URL)."""
    page.wait_for_load_state("domcontentloaded")
    page.get_by_role("button", name=name).click()


def test_new_account_completes_onboarding_and_lands_in_chat(not_onboarded_page: Page) -> None:
    """Sport, About you skipped (so no model call leaves the stack), the summary, and confirm
    (ADR-0028, ticket #254). #256 extends it to two sports."""
    page = not_onboarded_page
    page.goto(f"{BASE_URL}/")
    expect(page).to_have_url(f"{BASE_URL}/onboarding/sport")

    _press(page, "Climbing")

    expect(page).to_have_url(f"{BASE_URL}/onboarding/about-you")
    _press(page, "Skip")

    expect(page).to_have_url(f"{BASE_URL}/onboarding/summary")
    expect(page.get_by_text("Skipped")).to_be_visible()
    expect(page.get_by_label("Age", exact=True)).to_have_value("")
    _press(page, "Looks right, build my plan")

    expect(page).to_have_url(f"{BASE_URL}/")
    expect(page.locator("#chat-content")).to_be_visible()

    page.get_by_label("Message").fill(MESSAGE)
    page.get_by_role("button", name="Send").click()
    expect(page.locator("#chat-history")).to_contain_text(MESSAGE)

    page.reload()
    expect(page.locator("#chat-history")).to_contain_text(MESSAGE)

    page.goto(f"{BASE_URL}/onboarding")
    expect(page).to_have_url(f"{BASE_URL}/")
