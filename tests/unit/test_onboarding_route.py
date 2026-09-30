"""Wires `GET`/`POST /onboarding/sports`, `/onboarding/availability` and `/onboarding/goals`
(PRD-0003 F6, B9, B10, ADR-0006, tickets #74, #75 and #76)."""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.goals import Goal
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.home import build_home_router
from ai_trainer.web.onboarding import build_onboarding_router
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class FakeUsersRepository:
    def __init__(self, *users: User) -> None:
        self._users = {user.id: user for user in users}

    def replace(self, user: User) -> None:
        self._users[user.id] = user

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
    def __init__(self, users: FakeUsersRepository | None = None) -> None:
        self.users = users
        self.sports: dict[UUID, list[str]] = {}
        self.availability: dict[UUID, dict[int, int]] = {}
        self.goals: dict[UUID, list[Goal]] = {}

    async def replace_sports(
        self,
        user_id: UUID,
        sport_ids: Sequence[str],
        *,
        general_goals_of: Sequence[str] = (),
        delete_goals_of: Sequence[str] = (),
    ) -> None:
        self.sports[user_id] = list(sport_ids)
        self.goals[user_id] = [
            goal.model_copy(update={"sport_id": None})
            if goal.sport_id in general_goals_of
            else goal
            for goal in self.goals.get(user_id, [])
            if goal.sport_id not in delete_goals_of
        ]

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

    async def finish_onboarding(self, user_id: UUID, timezone: str, when: datetime) -> None:
        assert self.users is not None
        user = await self.users.get(user_id)
        assert user is not None
        self.users.replace(user.model_copy(update={"timezone": timezone, "onboarded_at": when}))


def _client(*, onboarded: bool = False) -> tuple[TestClient, User, FakeOnboardingRepository]:
    user = User(
        id=uuid4(),
        sub="google-sub-1",
        email="athlete@example.com",
        name="Athlete",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=FROZEN_NOW,
        onboarded_at=FROZEN_NOW if onboarded else None,
    )
    session = Session(
        id=uuid4(),
        user_id=user.id,
        expires_at=FROZEN_NOW + timedelta(days=1),
        created_at=FROZEN_NOW,
    )
    users = FakeUsersRepository(user)
    onboarding = FakeOnboardingRepository(users)
    templates = build_templates()
    app = FastAPI()
    app.include_router(build_home_router(templates))
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


def _client_with_gym_goals() -> tuple[TestClient, User, FakeOnboardingRepository]:
    client, user, onboarding = _client()
    onboarding.sports[user.id] = ["climbing", "gym"]
    onboarding.goals[user.id] = [
        Goal(text="Send 8a+", sport_id="climbing"),
        Goal(text="Pull-up +10 kg", sport_id="gym"),
        Goal(text="Deadlift 150 kg", sport_id="gym"),
    ]
    return client, user, onboarding


def test_unticking_a_sport_with_goals_opens_the_dialog_listing_them_and_saves_nothing() -> None:
    client, user, onboarding = _client_with_gym_goals()

    response = client.post("/onboarding/sports", data={"sports": ["climbing"]})

    assert response.status_code == 200
    assert 'role="dialog"' in response.text
    assert "Drop Gym?" in response.text
    assert "Pull-up +10 kg" in response.text
    assert "Deadlift 150 kg" in response.text
    assert "Send 8a+" not in response.text
    assert 'name="goals_gym"' in response.text
    assert 'value="keep"' in response.text
    assert 'value="delete"' in response.text
    assert 'name="confirm_drop"' in response.text
    assert onboarding.sports[user.id] == ["climbing", "gym"]
    assert len(onboarding.goals[user.id]) == 3


def test_the_dialog_keeps_the_athletes_ticks_and_preselects_keep() -> None:
    client, _, _ = _client_with_gym_goals()

    response = client.post(
        "/onboarding/sports", data={"sports": ["climbing"]}, headers={"HX-Request": "true"}
    )

    assert "<html" not in response.text
    dialog = response.text[response.text.index('role="dialog"') :]
    keep_at = dialog.index('value="keep"')
    assert " checked" in dialog[keep_at : dialog.index(">", keep_at)]
    delete_at = dialog.index('value="delete"')
    assert " checked" not in dialog[delete_at : dialog.index(">", delete_at)]
    form = response.text[: response.text.index('role="dialog"')]
    gym_at = form.index('value="gym"')
    assert " checked" not in form[gym_at : form.index(">", gym_at)]
    climbing_at = form.index('value="climbing"')
    assert " checked" in form[climbing_at : form.index(">", climbing_at)]


def test_the_sports_and_continue_behind_the_dialog_cannot_be_reached() -> None:
    client, _, _ = _client_with_gym_goals()

    open_ = client.post("/onboarding/sports", data={"sports": ["climbing"]})
    plain = client.get("/onboarding/sports")

    assert open_.text.count("inert") == 2
    assert "inert" not in plain.text


def test_dropping_two_sports_with_goals_gets_one_choice_each() -> None:
    client, _, _ = _client_with_gym_goals()

    response = client.post("/onboarding/sports", data={"sports": ["cycling"]})

    assert "Drop 2 sports?" in response.text
    assert 'name="goals_climbing"' in response.text
    assert 'name="goals_gym"' in response.text


def test_the_dialogs_cancel_reloads_the_saved_step_without_posting() -> None:
    client, _, _ = _client_with_gym_goals()

    response = client.post("/onboarding/sports", data={"sports": ["climbing"]})

    cancel_at = response.text.index("Cancel")
    button = response.text[response.text.rindex("<button", 0, cancel_at) : cancel_at]
    assert 'hx-get="/onboarding/sports"' in button
    assert 'hx-target="#onboarding-step"' in button
    saved = client.get("/onboarding/sports")
    assert 'role="dialog"' not in saved.text
    gym_at = saved.text.index('value="gym"')
    assert " checked" in saved.text[gym_at : saved.text.index(">", gym_at)]


def test_confirming_keep_drops_the_sport_and_its_goals_move_to_general() -> None:
    client, user, onboarding = _client_with_gym_goals()

    response = client.post(
        "/onboarding/sports",
        data={"sports": ["climbing"], "confirm_drop": "1", "goals_gym": "keep"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding/availability"
    assert onboarding.sports[user.id] == ["climbing"]
    assert onboarding.goals[user.id] == [
        Goal(text="Send 8a+", sport_id="climbing"),
        Goal(text="Pull-up +10 kg"),
        Goal(text="Deadlift 150 kg"),
    ]
    goals = client.get("/onboarding/goals")
    general = goals.text[goals.text.index("General") :]
    assert "Pull-up +10 kg" in general
    assert "Deadlift 150 kg" in general


def test_confirming_delete_drops_the_sport_and_its_goals_only() -> None:
    client, user, onboarding = _client_with_gym_goals()

    response = client.post(
        "/onboarding/sports",
        data={"sports": ["climbing"], "confirm_drop": "1", "goals_gym": "delete"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/onboarding/availability"
    assert onboarding.sports[user.id] == ["climbing"]
    assert onboarding.goals[user.id] == [Goal(text="Send 8a+", sport_id="climbing")]


@pytest.mark.parametrize(
    "data",
    [
        {"sports": ["climbing"], "confirm_drop": "1"},
        {"sports": ["climbing"], "confirm_drop": "1", "goals_gym": "archive"},
        {"sports": ["climbing"], "confirm_drop": "1", "goals_gym": ""},
        {"sports": ["cycling"], "confirm_drop": "1", "goals_gym": "keep"},
    ],
)
def test_a_missing_or_unknown_choice_returns_422_and_changes_nothing(
    data: dict[str, str | list[str]],
) -> None:
    client, user, onboarding = _client_with_gym_goals()

    response = client.post("/onboarding/sports", data=data)

    assert response.status_code == 422
    assert onboarding.sports[user.id] == ["climbing", "gym"]
    assert len(onboarding.goals[user.id]) == 3
    assert [goal.sport_id for goal in onboarding.goals[user.id]] == ["climbing", "gym", "gym"]


@pytest.mark.parametrize(
    "body",
    [
        "sports=climbing&sports=gym&confirm_drop=1&goals_gym=archive",
        "sports=climbing&confirm_drop=1&goals_gym=archive&goals_gym=keep",
        "sports=climbing&confirm_drop=1&goals_gym=keep&goals_gym=archive",
        "sports=climbing&confirm_drop=1&goals_gym=keep&goals_cycling=KEEP",
    ],
)
def test_any_bad_or_repeated_choice_returns_422_even_for_a_sport_that_needs_none(
    body: str,
) -> None:
    client, user, onboarding = _client_with_gym_goals()

    response = client.post(
        "/onboarding/sports",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 422
    assert onboarding.sports[user.id] == ["climbing", "gym"]
    assert [goal.sport_id for goal in onboarding.goals[user.id]] == ["climbing", "gym", "gym"]


def test_unticking_a_sport_without_goals_goes_straight_to_step_two() -> None:
    client, user, onboarding = _client_with_gym_goals()
    onboarding.goals[user.id] = [Goal(text="Send 8a+", sport_id="climbing")]

    response = client.post(
        "/onboarding/sports", data={"sports": ["climbing"]}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding/availability"
    assert onboarding.sports[user.id] == ["climbing"]


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


def _input_value(html: str, weekday: int) -> str | None:
    """The `value` of the hidden field posted as `minutes_<weekday>`, or None when absent."""
    marker = f'name="minutes_{weekday}"'
    if marker not in html:
        return None
    tag = html[html.rindex("<input", 0, html.index(marker)) :]
    tag = tag[: tag.index(">")]
    return tag.split('value="', 1)[1].split('"', 1)[0]


def _chip(html: str, weekday: int) -> str:
    """The text of the day chip for `weekday`, whitespace collapsed."""
    start = html.index(f'data-day-chip="{weekday}"')
    inner = html[html.index(">", start) + 1 : html.index("</button>", start)]
    return " ".join(re.sub(r"<[^>]+>", " ", inner).split())


def _slider(html: str) -> str:
    start = html.index('type="range"')
    return html[html.rindex("<input", 0, start) : html.index(">", start)]


def test_one_slider_covers_rest_to_ten_hours_in_15_minute_steps() -> None:
    client, _, _ = _client()

    slider = _slider(client.get("/onboarding/availability").text)

    assert 'min="0"' in slider
    assert 'max="600"' in slider
    assert 'step="15"' in slider
    assert "name=" not in slider


def test_the_slider_starts_on_monday_when_nothing_is_saved() -> None:
    client, _, _ = _client()

    html = client.get("/onboarding/availability").text

    assert 'value="0"' in _slider(html)
    assert 'data-day-chip="0"' in html
    assert re.search(r'data-day-chip="0"[^>]*aria-pressed="true"', html)
    assert not re.search(r'data-day-chip="[1-6]"[^>]*aria-pressed="true"', html)


def test_each_chip_shows_its_own_duration_or_rest() -> None:
    client, user, onboarding = _client()
    onboarding.availability[user.id] = {0: 60, 2: 90, 4: 45, 5: 180}

    html = client.get("/onboarding/availability").text

    assert [_chip(html, d) for d in range(7)] == [
        "Mon 1h",
        "Tue Rest",
        "Wed 1h30",
        "Thu Rest",
        "Fri 45m",
        "Sat 3h",
        "Sun Rest",
    ]


def test_the_summary_counts_days_and_weekly_hours() -> None:
    client, user, onboarding = _client()
    onboarding.availability[user.id] = {0: 60, 2: 90, 5: 180}

    html = client.get("/onboarding/availability").text

    assert "3 days · 5 h 30 min a week" in " ".join(html.split())


def test_the_summary_with_one_day_is_singular() -> None:
    client, user, onboarding = _client()
    onboarding.availability[user.id] = {3: 60}

    html = client.get("/onboarding/availability").text

    assert "1 day · 1 h a week" in " ".join(html.split())


def test_the_slider_and_selected_chip_start_on_the_first_saved_day() -> None:
    client, user, onboarding = _client()
    onboarding.availability[user.id] = {2: 90, 5: 180}

    html = client.get("/onboarding/availability").text

    assert 'value="90"' in _slider(html)
    assert re.search(r'data-day-chip="2"[^>]*aria-pressed="true"', html)


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


async def _stored(onboarding: FakeOnboardingRepository, user: User) -> User:
    assert onboarding.users is not None
    stored = await onboarding.users.get(user.id)
    assert stored is not None
    return stored


def test_timezone_step_is_a_full_page_prefilled_with_utc() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/timezone")

    assert response.status_code == 200
    assert "<html" in response.text
    assert "4 / 4" in response.text
    assert 'name="timezone"' in response.text
    assert 'value="UTC"' in response.text
    assert "/static/js/onboarding_timezone.js" in response.text


def test_timezone_step_offers_iana_names_and_leaves_out_the_grade_scale() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/timezone")

    assert 'value="Europe/Warsaw"' in response.text
    assert "Climbing grades" not in response.text
    assert "Grade scale" not in response.text


def test_timezone_step_with_hx_request_returns_a_partial() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/timezone", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'name="timezone"' in response.text


async def test_confirming_a_zone_stores_it_finishes_onboarding_and_lands_on_home() -> None:
    client, user, onboarding = _client()

    response = client.post(
        "/onboarding/timezone", data={"timezone": "Europe/Warsaw"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    stored = await _stored(onboarding, user)
    assert stored.timezone == "Europe/Warsaw"
    assert stored.onboarded_at == FROZEN_NOW


def test_confirming_through_htmx_redirects_with_hx_redirect() -> None:
    client, _, _ = _client()

    response = client.post(
        "/onboarding/timezone",
        data={"timezone": "Europe/Warsaw"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/"


async def test_a_posted_unknown_zone_shows_an_inline_error_and_changes_nothing() -> None:
    client, user, onboarding = _client()

    response = client.post("/onboarding/timezone", data={"timezone": "Mars/Olympus"})

    assert response.status_code == 422
    assert "Choose a timezone from the list" in response.text
    assert 'aria-invalid="true"' in response.text
    assert "Mars/Olympus" not in response.text
    stored = await _stored(onboarding, user)
    assert stored.timezone == "UTC"
    assert stored.onboarded_at is None


def test_a_missing_zone_field_is_the_same_inline_error() -> None:
    client, _, _ = _client()

    response = client.post("/onboarding/timezone", data={})

    assert response.status_code == 422
    assert "Choose a timezone from the list" in response.text


def test_a_failed_timezone_post_through_htmx_returns_the_partial() -> None:
    client, _, _ = _client()

    response = client.post(
        "/onboarding/timezone", data={"timezone": "Nope"}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 422
    assert "<html" not in response.text
    assert "Choose a timezone from the list" in response.text


def test_after_finishing_home_no_longer_redirects_to_onboarding() -> None:
    client, _, _ = _client()
    assert client.get("/", follow_redirects=False).status_code == 303
    client.post("/onboarding/timezone", data={"timezone": "UTC"})

    response = client.get("/", follow_redirects=False)

    assert response.status_code != 303


def test_a_finished_athlete_opening_onboarding_is_sent_home() -> None:
    client, _, _ = _client(onboarded=True)

    response = client.get("/onboarding", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_finished_athlete_opening_the_timezone_step_is_sent_home() -> None:
    client, _, _ = _client(onboarded=True)

    response = client.get("/onboarding/timezone", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_finished_athlete_is_sent_home_through_htmx_with_hx_redirect() -> None:
    client, _, _ = _client(onboarded=True)

    response = client.get("/onboarding", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/"


def test_an_unfinished_athlete_opening_onboarding_starts_at_the_sports_step() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding/sports"
