"""The fitness and mobility session journey (PRD 0013, ticket #634): a stretch, a rower and a
3-round circuit are confirmed from a draft, read back in History, plotted in the rower's progress
and removed on delete. Each test seeds fresh data (`tests/e2e/fitness_session_seed.py`), so none
depends on another. No model is called: the draft is stored by the seeder."""

import json
import re

from playwright.sync_api import Page, expect

from tests.e2e.conftest import BASE_URL

ROWER = "Rowing machine"
SWING = "Kettlebell swing"
DRAFT_DAY = r"Wed\s*25\s*Nov"
EARLIER_DAY = r"Wed\s*18\s*Nov"
PACE = "2:05 /500 m"


def _confirm(page: Page) -> None:
    page.goto(f"{BASE_URL}/")
    page.get_by_role("article", name="Draft session").get_by_role("button", name="Confirm").click()
    expect(page.locator("[data-confirmed-draft]")).to_be_visible()


def _open_saved(page: Page, day: str) -> None:
    page.goto(f"{BASE_URL}/history")
    page.get_by_role("link", name=re.compile(day)).first.click()
    expect(page.get_by_role("region", name="Saved session")).to_be_visible()


def _open_rower_progress(page: Page) -> None:
    page.get_by_role("region", name="Saved session").get_by_role(
        "link", name=re.compile(ROWER)
    ).click()
    expect(page.get_by_role("heading", level=1, name=ROWER)).to_be_visible()


def test_fitness_confirmed_session_reads_back_in_history(fitness_session_page: Page) -> None:
    page = fitness_session_page
    _confirm(page)
    _open_saved(page, DRAFT_DAY)
    session = page.get_by_role("region", name="Saved session")
    expect(session).to_contain_text("45 s")
    expect(session).to_contain_text(PACE)
    expect(session).to_contain_text("3 rounds")
    expect(session.locator("[data-circuit]")).to_be_visible()


def test_fitness_rower_progress_plots_the_faster_session_higher(
    fitness_session_page: Page,
) -> None:
    page = fitness_session_page
    _confirm(page)
    _open_saved(page, DRAFT_DAY)
    _open_rower_progress(page)
    # The canvas draws from its `data-chart` payload, oldest first: 18 Nov slower, 25 Nov faster.
    payload = page.locator("#progress-chart").get_attribute("data-chart")
    assert payload is not None, "the progress chart has no data-chart payload"
    chart = json.loads(payload)
    assert chart["unit"] == "km/h"
    assert len(chart["values"]) == 2
    assert chart["values"][1] > chart["values"][0]
    rows = page.get_by_role("table", name="Sessions with this exercise").get_by_role("row")
    expect(rows).to_have_count(3)
    expect(rows.nth(1)).to_contain_text("25 Nov")
    expect(rows.nth(1)).to_contain_text(PACE)


def test_fitness_logged_card_shows_duration_and_rpe(fitness_session_page: Page) -> None:
    page = fitness_session_page
    _confirm(page)
    meta = page.locator("[data-confirmed-draft] [data-session-meta]")
    expect(meta).to_contain_text("50 min")
    expect(meta).to_contain_text("RPE 7")


def test_fitness_deleting_the_session_removes_its_circuit_and_sets(
    fitness_session_page: Page,
) -> None:
    page = fitness_session_page
    _confirm(page)
    _open_saved(page, DRAFT_DAY)
    content = page.locator("#history-content")
    # The list shows the circuit and its swings before the delete, so their absence after it counts.
    listed = content.get_by_role("link", name=re.compile(DRAFT_DAY))
    expect(listed.locator("[data-circuit]")).to_have_count(1)
    expect(listed).to_contain_text("3 rounds")
    expect(listed).to_contain_text(SWING)
    page.get_by_role("region", name="Saved session").get_by_role("button", name="Delete").click()
    dialog = page.get_by_role("alertdialog", name="Delete this session?")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Delete session").click()
    expect(content.get_by_role("link", name=re.compile(DRAFT_DAY))).to_have_count(0)
    expect(content.locator("[data-circuit]")).to_have_count(0)
    expect(content).not_to_contain_text("3 rounds")
    expect(content).not_to_contain_text(SWING)
    expect(content.get_by_role("link", name=re.compile(EARLIER_DAY))).to_have_count(1)
    content.get_by_role("link", name=re.compile(EARLIER_DAY)).click()
    _open_rower_progress(page)
    table = page.get_by_role("table", name="Sessions with this exercise")
    expect(table.get_by_role("row")).to_have_count(2)
    expect(table).not_to_contain_text(PACE)
