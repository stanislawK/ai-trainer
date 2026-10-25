"""A new account walks through onboarding with two sports into the chat (PRD 0008 F1, F6, G11;
tickets #86, #254, #255, #256).

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


def test_new_account_onboards_two_sports_and_lands_in_chat(not_onboarded_page: Page) -> None:
    """Climbing, About you and its three steps, then Gym from "Add another sport?" with only its
    three steps, all skipped (so no model call leaves the stack), then the summary with a section
    per sport, and confirm (ADR-0028, tickets #254-#256)."""
    page = not_onboarded_page
    page.goto(f"{BASE_URL}/")
    expect(page).to_have_url(f"{BASE_URL}/onboarding/sport")

    _press(page, "Climbing")
    expect(page).to_have_url(f"{BASE_URL}/onboarding/about-you")
    _press(page, "Skip")
    for step in ("goals", "level", "schedule"):
        expect(page).to_have_url(f"{BASE_URL}/onboarding/climbing/{step}")
        _press(page, "Skip")

    expect(page).to_have_url(f"{BASE_URL}/onboarding/more")
    expect(page.get_by_role("button", name="Climbing")).to_have_count(0)
    _press(page, "Gym")
    # No About you for a second sport.
    for step in ("goals", "level", "schedule"):
        expect(page).to_have_url(f"{BASE_URL}/onboarding/gym/{step}")
        _press(page, "Skip")

    expect(page).to_have_url(f"{BASE_URL}/onboarding/more")
    page.get_by_role("link", name="No, show my summary").click()

    expect(page).to_have_url(f"{BASE_URL}/onboarding/summary")
    # About you, Climbing and Gym, all skipped.
    expect(page.get_by_text("Skipped")).to_have_count(3)
    expect(page.locator('section[aria-label="Climbing"]')).to_be_visible()
    expect(page.locator('section[aria-label="Gym"]')).to_be_visible()
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
