"""Wires `GET`/`POST /onboarding/sports`, `/onboarding/availability` and `/onboarding/goals`
(PRD-0003 F6, B9, B10, ADR-0006, tickets #74, #75 and #76)."""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.goals import Goal
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.onboarding import build_onboarding_router
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class FakeUsersRepository:
    def __init__(self, *users: User) -> None:
        self._users = {user.id: user for user in users}

    async def get_by_sub(self, sub: str) -> User | None:
        raise NotImplementedError

    async def get(self, user_id: UUID) -> User | None:
        return self._users.get(user_id)

    async def create(self, new_user: NewUser) -> User:
        raise NotImplementedError

    async def list_all(self) -> list[User]:
        raise NotImplementedError

    async def delete(self, user_id: UUID) -> None:
        raise NotImplementedError


class FakeSessionsRepository:
    def __init__(self, *sessions: Session) -> None:
        self._sessions = {session.id: session for session in sessions}

    async def create(self, new_session: NewSession) -> Session:
        raise NotImplementedError

    async def get(self, session_id: UUID) -> Session | None:
        return self._sessions.get(session_id)

    async def delete(self, session_id: UUID) -> None:
        raise NotImplementedError

    async def delete_for_user(self, user_id: UUID) -> None:
        raise NotImplementedError


class FakeOnboardingRepository:
    def __init__(self) -> None:
        self.sports: dict[UUID, list[str]] = {}
        self.availability: dict[UUID, dict[int, int]] = {}
        self.goals: dict[UUID, list[Goal]] = {}

    async def replace_sports(self, user_id: UUID, sport_ids: Sequence[str]) -> None:
        self.sports[user_id] = list(sport_ids)

    async def list_sports(self, user_id: UUID) -> Sequence[str]:
        return self.sports.get(user_id, [])

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        self.availability[user_id] = dict(minutes_by_weekday)

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]:
        return self.availability.get(user_id, {})

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        self.goals[user_id] = list(goals)

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]:
        return self.goals.get(user_id, [])

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None:
        raise NotImplementedError


def _client() -> tuple[TestClient, User, FakeOnboardingRepository]:
    user = User(
        id=uuid4(),
        sub="google-sub-1",
        email="athlete@example.com",
        name="Athlete",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=FROZEN_NOW,
    )
    session = Session(
        id=uuid4(),
        user_id=user.id,
        expires_at=FROZEN_NOW + timedelta(days=1),
        created_at=FROZEN_NOW,
    )
    users = FakeUsersRepository(user)
    onboarding = FakeOnboardingRepository()
    templates = build_templates()
    app = FastAPI()
    app.include_router(
        build_onboarding_router(
            templates,
            registry=default_sport_registry(),
            onboarding=onboarding,
            clock=FakeClock(),
        )
    )
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=FakeSessionsRepository(session),
        clock=FakeClock(),
        templates=templates,
        admin_emails=[],
    )
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    return client, user, onboarding


def test_sports_step_is_a_full_page_listing_every_registry_sport() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/sports")

    assert response.status_code == 200
    assert "<html" in response.text
    assert "What do you train?" in response.text
    for sport_id in ("climbing", "gym", "cycling"):
        assert f'value="{sport_id}"' in response.text
    for label in ("Climbing", "Gym", "Cycling"):
        assert label in response.text
    assert "1 / 4" in response.text


def test_sports_step_shows_each_sports_registry_icon() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/sports")

    assert response.text.count("<svg") >= 3


def test_sports_step_with_hx_request_returns_a_partial() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/sports", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'value="climbing"' in response.text


def test_sports_step_starts_with_nothing_checked() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/sports")

    assert " checked" not in response.text


def test_sports_step_prechecks_the_sports_already_stored() -> None:
    client, user, onboarding = _client()
    onboarding.sports[user.id] = ["gym"]

    response = client.get("/onboarding/sports")

    assert response.text.count(" checked") == 1
    gym_at = response.text.index('value="gym"')
    tag_end = response.text.index(">", gym_at)
    assert " checked" in response.text[gym_at:tag_end]


def test_picking_climbing_and_gym_stores_exactly_those_and_moves_to_step_two() -> None:
    client, user, onboarding = _client()

    response = client.post(
        "/onboarding/sports", data={"sports": ["climbing", "gym"]}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding/availability"
    assert onboarding.sports == {user.id: ["climbing", "gym"]}


def test_picking_sports_through_htmx_redirects_with_hx_redirect() -> None:
    client, _, _ = _client()

    response = client.post(
        "/onboarding/sports", data={"sports": ["gym"]}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/onboarding/availability"


def test_picking_again_replaces_the_earlier_choice() -> None:
    client, user, onboarding = _client()
    client.post("/onboarding/sports", data={"sports": ["climbing", "gym"]})

    client.post("/onboarding/sports", data={"sports": ["gym"]})

    assert onboarding.sports == {user.id: ["gym"]}


def test_continuing_with_nothing_picked_shows_an_inline_error_and_stores_nothing() -> None:
    client, _, onboarding = _client()

    response = client.post("/onboarding/sports", data={})

    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "Pick at least one sport" in response.text
    assert 'value="climbing"' in response.text
    assert onboarding.sports == {}


def test_continuing_with_nothing_picked_through_htmx_returns_the_error_partial() -> None:
    client, _, onboarding = _client()

    response = client.post("/onboarding/sports", data={}, headers={"HX-Request": "true"})

    assert response.status_code == 422
    assert "<html" not in response.text
    assert "Pick at least one sport" in response.text
    assert onboarding.sports == {}


def test_an_unknown_sport_id_is_rejected_with_422_and_stores_nothing() -> None:
    client, _, onboarding = _client()

    response = client.post("/onboarding/sports", data={"sports": ["climbing", "chess"]})

    assert response.status_code == 422
    assert onboarding.sports == {}


def test_availability_step_is_step_two_with_a_way_back_and_a_row_per_weekday() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/availability")

    assert response.status_code == 200
    assert "<html" in response.text
    assert "When can you train?" in response.text
    assert "2 / 4" in response.text
    assert 'href="/onboarding/sports"' in response.text
    for weekday in range(7):
        assert f'name="minutes_{weekday}"' in response.text
    for label in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        assert label in response.text


def test_availability_step_with_hx_request_returns_a_partial() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/availability", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'name="minutes_0"' in response.text


def _select(html: str, weekday: int) -> str:
    start = html.index(f'name="minutes_{weekday}"')
    return html[html.rindex("<select", 0, start) : html.index("</select>", start)]


def _input_value(html: str, weekday: int) -> str | None:
    """The `value` of the option selected for `weekday`, or None when none is."""
    for option in _select(html, weekday).split("<option")[1:]:
        if " selected" in option.split(">", 1)[0]:
            return option.split('value="', 1)[1].split('"', 1)[0]
    return None


def test_each_day_offers_15_minute_steps_from_rest_day_to_ten_hours() -> None:
    client, _, _ = _client()

    html = _select(client.get("/onboarding/availability").text, 2)

    values = [int(v.split('"', 1)[0]) for v in html.split('value="')[1:]]
    assert values == list(range(0, 601, 15))
    for label in ("Rest day", "15 min", "1 h", "1 h 30 min", "1 h 45 min", "10 h"):
        assert f">{label}<" in html.replace("\n", "").replace("  ", "")


def test_availability_step_starts_every_day_at_zero() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/availability")

    assert [_input_value(response.text, d) for d in range(7)] == ["0"] * 7


def test_revisiting_availability_shows_the_saved_minutes() -> None:
    client, user, onboarding = _client()
    onboarding.availability[user.id] = {0: 60, 2: 90, 5: 180}

    response = client.get("/onboarding/availability")

    assert [_input_value(response.text, d) for d in range(7)] == [
        "60",
        "0",
        "90",
        "0",
        "0",
        "180",
        "0",
    ]


def test_saving_mon_wed_sat_stores_three_rows_and_moves_to_goals() -> None:
    client, user, onboarding = _client()

    response = client.post(
        "/onboarding/availability",
        data={
            "minutes_0": "60",
            "minutes_1": "0",
            "minutes_2": "90",
            "minutes_3": "0",
            "minutes_4": "0",
            "minutes_5": "180",
            "minutes_6": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding/goals"
    assert onboarding.availability == {user.id: {0: 60, 2: 90, 5: 180}}


def test_saving_availability_through_htmx_redirects_with_hx_redirect() -> None:
    client, _, _ = _client()

    response = client.post(
        "/onboarding/availability", data={"minutes_0": "45"}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/onboarding/goals"


def test_zero_on_every_day_shows_an_inline_error_and_stores_nothing() -> None:
    client, _, onboarding = _client()

    response = client.post("/onboarding/availability", data={f"minutes_{d}": "0" for d in range(7)})

    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "at least one day" in response.text
    assert onboarding.availability == {}


def test_a_failed_save_leaves_earlier_availability_stored() -> None:
    client, user, onboarding = _client()
    onboarding.availability[user.id] = {0: 60}

    client.post("/onboarding/availability", data={f"minutes_{d}": "0" for d in range(7)})

    assert onboarding.availability == {user.id: {0: 60}}


def test_601_minutes_shows_an_inline_error_on_that_day_and_keeps_the_other_days() -> None:
    client, _, onboarding = _client()

    response = client.post("/onboarding/availability", data={"minutes_0": "60", "minutes_3": "601"})

    assert response.status_code == 422
    assert "between 0 and 10 hours" in response.text
    assert 'aria-invalid="true"' in response.text
    assert _input_value(response.text, 0) == "60"
    assert onboarding.availability == {}


def test_a_negative_value_shows_an_inline_error() -> None:
    client, _, onboarding = _client()

    response = client.post("/onboarding/availability", data={"minutes_1": "-15"})

    assert response.status_code == 422
    assert "between 0 and 10 hours" in response.text
    assert onboarding.availability == {}


def test_a_failed_save_through_htmx_returns_the_partial() -> None:
    client, _, onboarding = _client()

    response = client.post(
        "/onboarding/availability", data={"minutes_1": "-15"}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 422
    assert "<html" not in response.text
    assert "between 0 and 10 hours" in response.text
    assert onboarding.availability == {}


def test_availability_is_stored_for_the_signed_in_user_only() -> None:
    client, user, onboarding = _client()
    other = uuid4()
    onboarding.availability[other] = {4: 30}

    client.post("/onboarding/availability", data={"minutes_0": "60"})

    assert onboarding.availability == {other: {4: 30}, user.id: {0: 60}}


def test_a_missing_field_counts_as_zero() -> None:
    client, user, onboarding = _client()

    client.post("/onboarding/availability", data={"minutes_2": "30"})

    assert onboarding.availability == {user.id: {2: 30}}


def test_continuing_with_nothing_picked_leaves_an_earlier_choice_stored() -> None:
    client, user, onboarding = _client()
    onboarding.sports[user.id] = ["climbing", "gym"]

    response = client.post("/onboarding/sports", data={})

    assert response.status_code == 422
    assert onboarding.sports == {user.id: ["climbing", "gym"]}


# Goals (tickets #76 and #99). FROZEN_NOW is 12:00 UTC on 2026-09-29, when UTC-12 has just
# reached the 29th: the earliest "today" on Earth, so the 29th is the earliest target date
# accepted. Goals sit in one card per picked sport plus General; every row posts its own
# `goal_sport` (empty for General), `goal_text` and `goal_date`.


def _picked(onboarding: FakeOnboardingRepository, user: User, *sports: str) -> None:
    onboarding.sports[user.id] = list(sports)


def _cards(html: str) -> dict[str, str]:
    """Each goal card's markup keyed by its sport ID ("" for General), in page order. The row
    `<template>` is not part of a card."""
    html = re.sub(r"<template.*?</template>", "", html, flags=re.DOTALL)
    cards: dict[str, str] = {}
    for chunk in html.split("data-goal-card")[1:]:
        sport = chunk.split('data-sport="', 1)[1].split('"', 1)[0]
        cards[sport] = chunk
    return cards


def _goal_rows(html: str) -> list[str]:
    """Each posted goal row's markup, in page order across all cards."""
    html = re.sub(r"<template.*?</template>", "", html, flags=re.DOTALL)
    return html.split("data-goal-row")[1:]


def test_goals_step_is_step_three_with_a_way_back() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.get("/onboarding/goals")

    assert response.status_code == 200
    assert "<html" in response.text
    assert "What are you working toward?" in response.text
    assert "3 / 4" in response.text
    assert 'href="/onboarding/availability"' in response.text


def test_climbing_and_gym_picked_show_climbing_gym_and_general_cards_and_no_cycling() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")

    response = client.get("/onboarding/goals")

    cards = _cards(response.text)
    assert list(cards) == ["climbing", "gym", ""]
    assert "Climbing" in cards["climbing"]
    assert "Gym" in cards["gym"]
    assert "General" in cards[""]
    assert "Cycling" not in response.text


def test_cards_follow_the_registry_order_not_the_order_picked() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "cycling", "climbing")

    cards = _cards(client.get("/onboarding/goals").text)

    assert list(cards) == ["climbing", "cycling", ""]


def test_each_empty_card_starts_with_one_row_carrying_its_sport() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    cards = _cards(client.get("/onboarding/goals").text)

    for sport, card in cards.items():
        assert card.count("data-goal-row") == 1
        assert f'name="goal_sport" value="{sport}"' in card
        assert 'name="goal_text"' in card
        assert 'name="goal_date"' in card
        assert 'maxlength="200"' in card
        assert 'min="2026-09-29"' in card
        assert "data-goal-add" in card


def test_goals_step_carries_a_row_template_and_the_script() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.get("/onboarding/goals")

    assert "<template" in response.text
    assert "data-goal-remove" in response.text
    assert "/static/js/onboarding_goals.js" in response.text


def test_goals_step_with_hx_request_returns_a_partial() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.get("/onboarding/goals", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'name="goal_text"' in response.text


def test_revisiting_goals_shows_each_saved_goal_in_its_card() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")
    onboarding.goals[user.id] = [
        Goal(text="Send 8a+ by spring", target_date=date(2027, 4, 30), sport_id="climbing"),
        Goal(text="Pull-up with +10 kg", sport_id="gym"),
        Goal(text="Train consistently"),
    ]

    cards = _cards(client.get("/onboarding/goals").text)

    assert 'value="Send 8a+ by spring"' in cards["climbing"]
    assert 'value="2027-04-30"' in cards["climbing"]
    assert 'value="Pull-up with +10 kg"' in cards["gym"]
    assert 'value="Train consistently"' in cards[""]
    assert "Send 8a+" not in cards["gym"]


def test_goals_stored_before_sports_existed_appear_under_general() -> None:
    # #76 stored goals with no sport; the migration leaves them NULL.
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")
    onboarding.goals[user.id] = [Goal(text="Ride 100 km"), Goal(text="Stay consistent")]

    cards = _cards(client.get("/onboarding/goals").text)

    assert 'value="Ride 100 km"' in cards[""]
    assert 'value="Stay consistent"' in cards[""]
    assert 'value="Ride 100 km"' not in cards["climbing"]


def test_a_saved_goal_of_a_sport_no_longer_picked_shows_under_general() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "gym")
    onboarding.goals[user.id] = [Goal(text="Send 8a+", sport_id="climbing")]

    cards = _cards(client.get("/onboarding/goals").text)

    assert list(cards) == ["gym", ""]
    assert 'value="Send 8a+"' in cards[""]


def test_a_climbing_a_dated_gym_and_a_general_goal_are_stored_and_the_flow_moves_to_timezone() -> (
    None
):
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")

    response = client.post(
        "/onboarding/goals",
        data={
            "goal_sport": ["climbing", "gym", ""],
            "goal_text": ["Send 8a+ by spring", "Pull-up with +10 kg", "Train consistently"],
            "goal_date": ["", "2027-06-30", ""],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding/timezone"
    assert onboarding.goals == {
        user.id: [
            Goal(text="Send 8a+ by spring", sport_id="climbing"),
            Goal(text="Pull-up with +10 kg", target_date=date(2027, 6, 30), sport_id="gym"),
            Goal(text="Train consistently"),
        ]
    }


def test_saving_goals_through_htmx_redirects_with_hx_redirect() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.post(
        "/onboarding/goals",
        data={"goal_sport": ["climbing"], "goal_text": ["Climb 7a"], "goal_date": [""]},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/onboarding/timezone"


def test_a_goal_for_an_unpicked_sport_returns_422_and_stores_nothing() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")

    response = client.post(
        "/onboarding/goals",
        data={
            "goal_sport": ["climbing", "cycling"],
            "goal_text": ["Send 8a+", "Ride 100 km"],
            "goal_date": ["", ""],
        },
    )

    assert response.status_code == 422
    assert onboarding.goals == {}


def test_a_goal_for_an_unknown_sport_returns_422_and_stores_nothing() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.post(
        "/onboarding/goals",
        data={"goal_sport": ["chess"], "goal_text": ["Win"], "goal_date": [""]},
    )

    assert response.status_code == 422
    assert onboarding.goals == {}


def test_a_row_without_a_sport_field_is_a_general_goal() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    client.post("/onboarding/goals", data={"goal_text": ["Stay consistent"], "goal_date": [""]})

    assert onboarding.goals[user.id] == [Goal(text="Stay consistent")]


def test_all_cards_empty_shows_an_inline_error_and_stores_nothing() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")

    response = client.post(
        "/onboarding/goals",
        data={
            "goal_sport": ["climbing", "gym", ""],
            "goal_text": ["", "", ""],
            "goal_date": ["", "", ""],
        },
    )

    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "Add at least one goal" in response.text
    assert list(_cards(response.text)) == ["climbing", "gym", ""]
    assert onboarding.goals == {}


def test_no_rows_posted_at_all_shows_the_inline_error() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.post("/onboarding/goals", data={})

    assert response.status_code == 422
    assert "Add at least one goal" in response.text
    assert list(_cards(response.text)) == ["climbing", ""]
    assert onboarding.goals == {}


def test_empty_rows_beside_a_filled_one_are_ignored() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")

    response = client.post(
        "/onboarding/goals",
        data={
            "goal_sport": ["climbing", "gym", ""],
            "goal_text": ["", "", "Train consistently"],
            "goal_date": ["", "", ""],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert onboarding.goals[user.id] == [Goal(text="Train consistently")]


def test_a_past_target_date_shows_an_inline_error_in_its_card_and_stores_nothing() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")

    response = client.post(
        "/onboarding/goals",
        data={
            "goal_sport": ["climbing", "gym"],
            "goal_text": ["Climb 7a", "Pull-up"],
            "goal_date": ["", "2026-09-28"],
        },
    )

    assert response.status_code == 422
    cards = _cards(response.text)
    assert "in the past" not in cards["climbing"]
    assert "in the past" in cards["gym"]
    assert 'aria-invalid="true"' in cards["gym"]
    assert 'value="Pull-up"' in cards["gym"]
    assert 'value="2026-09-28"' in cards["gym"]
    assert onboarding.goals == {}


def test_a_past_target_date_through_htmx_returns_the_partial_with_the_row_error() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.post(
        "/onboarding/goals",
        data={"goal_sport": ["climbing"], "goal_text": ["Climb 7a"], "goal_date": ["2026-09-28"]},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert "<html" not in response.text
    assert "in the past" in _goal_rows(response.text)[0]
    assert onboarding.goals == {}


def test_todays_date_is_accepted() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.post(
        "/onboarding/goals",
        data={"goal_sport": ["climbing"], "goal_text": ["Climb 7a"], "goal_date": ["2026-09-29"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert onboarding.goals[user.id] == [
        Goal(text="Climb 7a", target_date=date(2026, 9, 29), sport_id="climbing")
    ]


def test_a_goal_with_a_date_but_no_text_shows_an_inline_error_on_that_row() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.post(
        "/onboarding/goals",
        data={
            "goal_sport": ["climbing", ""],
            "goal_text": ["Climb 7a", "   "],
            "goal_date": ["", "2027-01-01"],
        },
    )

    assert response.status_code == 422
    cards = _cards(response.text)
    assert "Write your goal" not in cards["climbing"]
    assert "Write your goal" in cards[""]
    assert 'aria-invalid="true"' in cards[""]
    assert onboarding.goals == {}


def test_a_goal_over_200_characters_shows_an_inline_error() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.post(
        "/onboarding/goals",
        data={"goal_sport": ["climbing"], "goal_text": ["x" * 201], "goal_date": [""]},
    )

    assert response.status_code == 422
    assert "200 characters" in _cards(response.text)["climbing"]
    assert onboarding.goals == {}


def test_a_malformed_date_shows_an_inline_error() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.post(
        "/onboarding/goals",
        data={"goal_sport": [""], "goal_text": ["Climb 7a"], "goal_date": ["someday"]},
    )

    assert response.status_code == 422
    assert "Pick a valid date" in _cards(response.text)[""]
    assert onboarding.goals == {}


def test_a_row_removed_before_continuing_is_not_stored() -> None:
    # The page had three rows; the athlete removed the middle one, so the browser posts two.
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")
    client.post(
        "/onboarding/goals",
        data={
            "goal_sport": ["climbing", "climbing", ""],
            "goal_text": ["First", "Second", "Third"],
            "goal_date": ["", "", ""],
        },
    )

    client.post(
        "/onboarding/goals",
        data={
            "goal_sport": ["climbing", ""],
            "goal_text": ["First", "Third"],
            "goal_date": ["", ""],
        },
    )

    assert onboarding.goals == {
        user.id: [Goal(text="First", sport_id="climbing"), Goal(text="Third")]
    }


def test_a_row_without_a_date_field_is_treated_as_undated() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    client.post(
        "/onboarding/goals",
        data={"goal_sport": ["climbing", ""], "goal_text": ["Climb 7a", "Ride 100 km"]},
    )

    assert onboarding.goals[user.id] == [
        Goal(text="Climb 7a", sport_id="climbing"),
        Goal(text="Ride 100 km"),
    ]


def test_a_failed_goals_save_through_htmx_returns_the_partial() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")

    response = client.post(
        "/onboarding/goals",
        data={"goal_sport": [""], "goal_text": [""], "goal_date": ["2027-01-01"]},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert "<html" not in response.text
    assert "Write your goal" in response.text
    assert onboarding.goals == {}


def test_a_failed_goals_save_leaves_earlier_goals_stored() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing")
    onboarding.goals[user.id] = [Goal(text="Kept")]

    client.post("/onboarding/goals", data={})

    assert onboarding.goals == {user.id: [Goal(text="Kept")]}


def test_the_htmx_partial_shows_the_same_cards_as_the_full_page() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")

    response = client.get("/onboarding/goals", headers={"HX-Request": "true"})

    assert list(_cards(response.text)) == ["climbing", "gym", ""]


def test_resaving_a_goal_of_a_dropped_sport_stores_it_as_a_general_goal() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "gym")
    onboarding.goals[user.id] = [Goal(text="Send 8a+", sport_id="climbing")]
    page = client.get("/onboarding/goals").text
    assert 'value="Send 8a+"' in _cards(page)[""]

    client.post(
        "/onboarding/goals",
        data={"goal_sport": [""], "goal_text": ["Send 8a+"], "goal_date": [""]},
        follow_redirects=False,
    )

    assert onboarding.goals[user.id] == [Goal(text="Send 8a+")]


def test_each_card_suggests_an_example_goal_for_its_own_sport() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym", "cycling")

    cards = _cards(client.get("/onboarding/goals").text)

    assert 'placeholder="e.g. Send a 7a"' in cards["climbing"]
    assert 'placeholder="e.g. Pull-up +10 kg"' in cards["gym"]
    assert 'placeholder="e.g. Ride 100 km"' in cards["cycling"]
    assert 'placeholder="e.g. Stay consistent"' in cards[""]


def test_an_added_row_carries_its_cards_example_too() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "gym")

    html = client.get("/onboarding/goals").text

    template = re.search(r'data-sport="gym".*?<template[^>]*>(.*?)</template>', html, re.DOTALL)
    assert template is not None
    assert 'placeholder="e.g. Pull-up +10 kg"' in template.group(1)


def test_an_example_only_shows_where_it_fits() -> None:
    client, user, onboarding = _client()
    _picked(onboarding, user, "climbing", "gym")

    html = client.get("/onboarding/goals").text

    assert "Ride 100 km" not in html  # the cycling example stays on the cycling card
