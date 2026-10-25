"""Wires `GET`/`POST /onboarding/availability` and `/onboarding/timezone`
(PRD-0003 F6, B10, ADR-0006, tickets #75 and #77), until #256 removes them; #255 removed the
goals step."""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
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
from tests.chat_support import include_chat

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
    include_chat(app, templates)
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


def test_saving_mon_wed_sat_stores_three_rows_and_moves_to_the_timezone() -> None:
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
    assert response.headers["location"] == "/onboarding/timezone"
    assert onboarding.availability == {user.id: {0: 60, 2: 90, 5: 180}}


def test_saving_availability_through_htmx_redirects_with_hx_redirect() -> None:
    client, _, _ = _client()

    response = client.post(
        "/onboarding/availability", data={"minutes_0": "45"}, headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/onboarding/timezone"


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


def test_a_finished_athlete_opening_the_timezone_step_is_sent_home() -> None:
    client, _, _ = _client(onboarded=True)

    response = client.get("/onboarding/timezone", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
