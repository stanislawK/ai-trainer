"""Wires `GET`/`POST /onboarding/sports` and the step-2 placeholder (PRD-0003 F6, ADR-0006,
ticket #74)."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

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

    async def replace_sports(self, user_id: UUID, sport_ids: Sequence[str]) -> None:
        self.sports[user_id] = list(sport_ids)

    async def list_sports(self, user_id: UUID) -> Sequence[str]:
        return self.sports.get(user_id, [])

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
        build_onboarding_router(templates, registry=default_sport_registry(), onboarding=onboarding)
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


def test_availability_placeholder_is_step_two_with_a_way_back() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/availability")

    assert response.status_code == 200
    assert "2 / 4" in response.text
    assert 'href="/onboarding/sports"' in response.text


def test_availability_placeholder_with_hx_request_returns_a_partial() -> None:
    client, _, _ = _client()

    response = client.get("/onboarding/availability", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text


def test_continuing_with_nothing_picked_leaves_an_earlier_choice_stored() -> None:
    client, user, onboarding = _client()
    onboarding.sports[user.id] = ["climbing", "gym"]

    response = client.post("/onboarding/sports", data={})

    assert response.status_code == 422
    assert onboarding.sports == {user.id: ["climbing", "gym"]}
