"""The gym history journey (PRD 0011 M2g, ticket #519): History lists the athlete's gym
sessions, an edited set shows in history and in the exercise's progress, one exercise logged
under two typed names reads 6 kg then 7 kg, and deleting a session removes it from both views.
Each test seeds fresh data (`tests/e2e/gym_history_seed.py`), so none depends on another."""

import json
import re

from playwright.sync_api import Page, expect

from tests.e2e.conftest import BASE_URL

EXERCISE = "Dumbbell lateral raise"


def _open_later_session(page: Page) -> None:
    page.goto(f"{BASE_URL}/history")
    page.get_by_role("link", name=re.compile(r"Wed\s*4\s*Nov")).first.click()
    expect(page.get_by_role("region", name="Saved session")).to_be_visible()


def _open_progress(page: Page) -> None:
    page.get_by_role("region", name="Saved session").get_by_role(
        "link", name=re.compile(EXERCISE)
    ).click()
    expect(page.get_by_role("heading", level=1, name=EXERCISE)).to_be_visible()


def test_history_lists_the_seeded_gym_sessions(gym_history_page: Page) -> None:
    page = gym_history_page
    page.goto(f"{BASE_URL}/history")
    content = page.locator("#history-content")
    for day in (r"Wed\s*28\s*Oct", r"Wed\s*4\s*Nov"):
        session = content.get_by_role("link", name=re.compile(day))
        expect(session).to_have_count(1)
        expect(session).to_contain_text(EXERCISE)


def test_an_edited_set_shows_in_history_and_progress(gym_history_page: Page) -> None:
    page = gym_history_page
    _open_later_session(page)
    session = page.get_by_role("region", name="Saved session")
    expect(session).to_contain_text("7 kg")
    session.get_by_role("link", name="Edit").click()
    form = page.get_by_role("form", name="Edit session")
    form.get_by_label("kg", exact=True).first.fill("8")
    form.get_by_role("button", name="Save").click()
    session = page.get_by_role("region", name="Saved session")
    expect(session).to_be_visible()
    expect(session).to_contain_text("8 kg")
    _open_progress(page)
    table = page.get_by_role("table", name="Sessions with this exercise")
    # Newest first: the edited set is the session's best, so its row reads 10 @ 8 kg.
    expect(table.get_by_role("row").nth(1)).to_contain_text("4 Nov")
    expect(table.get_by_role("row").nth(1).get_by_role("cell").last).to_have_text("10 @ 8 kg")


def test_progress_shows_6_then_7_kg_across_two_typed_names(gym_history_page: Page) -> None:
    page = gym_history_page
    _open_later_session(page)
    _open_progress(page)
    # The chart runs oldest first: 6 kg, then 7 kg.
    # The canvas draws from its `data-chart` payload; there is no text to read the order from.
    payload = page.locator("#progress-chart").get_attribute("data-chart")
    assert payload is not None, "the progress chart has no data-chart payload"
    assert json.loads(payload).get("values") == [6, 7]
    # The table lists the same sessions newest first.
    rows = page.get_by_role("table", name="Sessions with this exercise").get_by_role("row")
    expect(rows).to_have_count(3)
    expect(rows.nth(1)).to_contain_text("4 Nov")
    expect(rows.nth(1)).to_contain_text("7 kg")
    expect(rows.nth(2)).to_contain_text("28 Oct")
    expect(rows.nth(2)).to_contain_text("6 kg")


def test_deleting_a_session_removes_it_from_history_and_progress(gym_history_page: Page) -> None:
    page = gym_history_page
    _open_later_session(page)
    page.get_by_role("region", name="Saved session").get_by_role("button", name="Delete").click()
    dialog = page.get_by_role("alertdialog", name="Delete this session?")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Delete session").click()
    content = page.locator("#history-content")
    expect(content.get_by_role("link", name=re.compile(r"Wed\s*4\s*Nov"))).to_have_count(0)
    expect(content.get_by_role("link", name=re.compile(r"Wed\s*28\s*Oct"))).to_have_count(1)
    page.get_by_role("link", name=re.compile(r"Wed\s*28\s*Oct")).click()
    _open_progress(page)
    table = page.get_by_role("table", name="Sessions with this exercise")
    expect(table).to_contain_text("6 kg")
    expect(table).not_to_contain_text("7 kg")
    expect(table.get_by_role("row")).to_have_count(2)
