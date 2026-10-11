"""A new account walks through onboarding into the chat (PRD-0003 F1, F6, G11; ticket #86).

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
    page = not_onboarded_page
    page.goto(f"{BASE_URL}/")
    expect(page).to_have_url(f"{BASE_URL}/onboarding/sports")

    page.get_by_label("Climbing", exact=False).check()
    page.get_by_label("Cycling", exact=False).check()
    _press(page, "Continue")

    expect(page).to_have_url(f"{BASE_URL}/onboarding/availability")
    page.locator("[data-day-chip='1']").click()
    page.locator("[data-day-slider]").fill("90")
    expect(page.locator("[data-day-chip='1'] [data-day-short]")).to_have_text("1h30")
    _press(page, "Continue")

    expect(page).to_have_url(f"{BASE_URL}/onboarding/goals")
    climbing_card = page.locator("[data-goal-card][data-sport='climbing']")
    climbing_card.locator("[data-goal-list]").get_by_label("Goal text").fill("Send a 7a")
    _press(page, "Continue")

    expect(page).to_have_url(f"{BASE_URL}/onboarding/timezone")
    _press(page, "Start chatting")

    expect(page).to_have_url(f"{BASE_URL}/")
    expect(page.locator("#chat-content")).to_be_visible()

    page.get_by_label("Message").fill(MESSAGE)
    page.get_by_role("button", name="Send").click()
    expect(page.locator("#chat-history")).to_contain_text(MESSAGE)

    page.reload()
    expect(page.locator("#chat-history")).to_contain_text(MESSAGE)

    page.goto(f"{BASE_URL}/onboarding")
    expect(page).to_have_url(f"{BASE_URL}/")


def test_continuing_the_sports_step_with_nothing_picked_shows_the_inline_error(
    not_onboarded_page: Page,
) -> None:
    page = not_onboarded_page
    page.goto(f"{BASE_URL}/onboarding/sports")

    _press(page, "Continue")

    expect(page.get_by_role("alert")).to_contain_text("Pick at least one sport to continue.")
    expect(page).to_have_url(f"{BASE_URL}/onboarding/sports")
