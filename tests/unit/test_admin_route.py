"""`GET /admin` and `POST /admin/users/{id}/status`: the admin user-management page (F7,
ADR-0005). Uses the same `ActiveUserGateMiddleware` fakes as `test_active_user_gate.py`."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import NewUser, User, UserNotFoundError, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


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
        return list(self._users.values())


class FakeSessionsRepository:
    def __init__(self, *sessions: Session) -> None:
        self._sessions = {session.id: session for session in sessions}
        self.deleted_for_user: list[UUID] = []

    async def create(self, new_session: NewSession) -> Session:
        raise NotImplementedError

    async def get(self, session_id: UUID) -> Session | None:
        return self._sessions.get(session_id)

    async def delete(self, session_id: UUID) -> None:
        raise NotImplementedError

    async def delete_for_user(self, user_id: UUID) -> None:
        self.deleted_for_user.append(user_id)


class FakeUserStatusChanger:
    """Mutates the same `FakeUsersRepository` a `GET` reads from, so a change is visible on
    the very next list, and records every call for the audit-trail assertions."""

    def __init__(self, users: FakeUsersRepository) -> None:
        self._users = users
        self.recorded: list[tuple[UUID, UUID, UserStatus, UserStatus]] = []

    async def change(
        self, *, target_user_id: UUID, new_status: UserStatus, actor_user_id: UUID
    ) -> tuple[User, UserStatus]:
        target = self._users._users.get(target_user_id)
        if target is None:
            raise UserNotFoundError(target_user_id)
        old_status = target.status
        updated = target.model_copy(update={"status": new_status})
        self._users._users[target_user_id] = updated
        self.recorded.append((actor_user_id, target_user_id, old_status, new_status))
        return updated, old_status


def _user(email: str = "athlete@example.com", status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(
        id=uuid4(),
        sub=f"google-sub-{email}",
        email=email,
        name="Athlete",
        locale="en",
        status=status,
        created_at=FROZEN_NOW,
    )


def _session(user: User) -> Session:
    return Session(
        id=uuid4(),
        user_id=user.id,
        expires_at=FROZEN_NOW + timedelta(days=1),
        created_at=FROZEN_NOW,
    )


_Client = tuple[
    TestClient,
    User,
    Session,
    FakeUsersRepository,
    FakeSessionsRepository,
    FakeUserStatusChanger,
]


def _client(*, admin_emails: list[str], extra_users: tuple[User, ...] = ()) -> _Client:
    user = _user()
    session = _session(user)
    users = FakeUsersRepository(user, *extra_users)
    sessions = FakeSessionsRepository(session)
    status_changer = FakeUserStatusChanger(users)
    app = FastAPI()
    templates = build_templates()
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=templates,
        admin_emails=admin_emails,
    )
    app.include_router(
        build_admin_router(
            templates=templates, users=users, sessions=sessions, status_changer=status_changer
        )
    )
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    return client, user, session, users, sessions, status_changer


def test_non_admin_gets_403_from_the_admin_page() -> None:
    client, _, _, _, _, _ = _client(admin_emails=[])

    response = client.get("/admin")

    assert response.status_code == 403
    assert "admin" in response.text.lower()


def test_admin_reaches_the_admin_page() -> None:
    client, admin, _, _, _, _ = _client(admin_emails=["athlete@example.com"])

    response = client.get("/admin")

    assert response.status_code == 200
    assert "Admin" in response.text
    assert admin.email in response.text


def test_non_admin_forbidden_response_is_an_htmx_partial_for_an_hx_request() -> None:
    client, _, _, _, _, _ = _client(admin_emails=[])

    response = client.get("/admin", headers={"HX-Request": "true"})

    assert response.status_code == 403
    assert "<html" not in response.text


def test_admin_sees_every_user_with_their_current_status() -> None:
    pending = _user("pending@example.com", UserStatus.PENDING)
    disabled = _user("disabled@example.com", UserStatus.DISABLED)
    client, admin, _, _, _, _ = _client(
        admin_emails=["athlete@example.com"], extra_users=(pending, disabled)
    )

    response = client.get("/admin")

    assert response.status_code == 200
    assert admin.email in response.text
    assert pending.email in response.text
    assert "Pending" in response.text
    assert disabled.email in response.text
    assert "Disabled" in response.text


def test_non_admin_post_gets_403() -> None:
    target = _user("target@example.com", UserStatus.PENDING)
    client, _, _, _, _, _ = _client(admin_emails=[], extra_users=(target,))

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "active"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 403


def test_non_admin_post_with_a_malformed_status_still_gets_403_not_422() -> None:
    """The admin gate must run before FastAPI would otherwise validate `status` against the
    `UserStatus` enum — a typed `Form(...)` parameter is validated before the handler body
    executes, which would let a non-admin trigger a 422 instead of a clean 403 just by sending
    a bogus value (ticket #16 review-gate skeptic finding)."""
    target = _user("target@example.com", UserStatus.PENDING)
    client, _, _, _, _, _ = _client(admin_emails=[], extra_users=(target,))

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "not-a-real-status"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 403


def test_non_admin_post_with_a_malformed_user_id_still_gets_403_not_422() -> None:
    client, _, _, _, _, _ = _client(admin_emails=[])

    response = client.post(
        "/admin/users/not-a-uuid/status",
        data={"status": "active"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 403


def test_admin_post_with_a_malformed_status_gets_422() -> None:
    target = _user("target@example.com", UserStatus.PENDING)
    client, _, _, _, _, status_changer = _client(
        admin_emails=["athlete@example.com"], extra_users=(target,)
    )

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "not-a-real-status"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert status_changer.recorded == []


def test_activating_a_pending_user_updates_their_status() -> None:
    target = _user("target@example.com", UserStatus.PENDING)
    client, _, _, users, _, _ = _client(admin_emails=["athlete@example.com"], extra_users=(target,))

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "active"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    updated = users._users[target.id]
    assert updated.status is UserStatus.ACTIVE


def test_disabling_an_active_user_revokes_their_sessions() -> None:
    target = _user("target@example.com", UserStatus.ACTIVE)
    client, _, _, _, sessions, _ = _client(
        admin_emails=["athlete@example.com"], extra_users=(target,)
    )

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "disabled"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert sessions.deleted_for_user == [target.id]


def test_admin_cannot_change_their_own_status() -> None:
    client, admin, _, users, sessions, status_changer = _client(
        admin_emails=["athlete@example.com"]
    )

    response = client.post(
        f"/admin/users/{admin.id}/status",
        data={"status": "disabled"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 400
    assert users._users[admin.id].status is UserStatus.ACTIVE
    assert sessions.deleted_for_user == []
    assert status_changer.recorded == []


def test_activating_a_user_without_hx_request_redirects_to_admin() -> None:
    target = _user("target@example.com", UserStatus.PENDING)
    client, _, _, _, _, _ = _client(admin_emails=["athlete@example.com"], extra_users=(target,))

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "active"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_changing_a_missing_user_returns_404() -> None:
    client, _, _, _, _, _ = _client(admin_emails=["athlete@example.com"])

    response = client.post(
        f"/admin/users/{uuid4()}/status",
        data={"status": "active"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 404


def test_every_change_is_recorded_with_actor_target_old_new_and_timestamp() -> None:
    target = _user("target@example.com", UserStatus.PENDING)
    client, admin, _, _, _, status_changer = _client(
        admin_emails=["athlete@example.com"], extra_users=(target,)
    )

    client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "active"},
        headers={"HX-Request": "true"},
    )

    assert len(status_changer.recorded) == 1
    actor_id, target_id, old_status, new_status = status_changer.recorded[0]
    assert actor_id == admin.id
    assert target_id == target.id
    assert old_status is UserStatus.PENDING
    assert new_status is UserStatus.ACTIVE


def test_removing_admin_email_demotes_on_the_next_request() -> None:
    user = _user()
    session = _session(user)
    users = FakeUsersRepository(user)
    sessions = FakeSessionsRepository(session)
    status_changer = FakeUserStatusChanger(users)
    templates = build_templates()

    def _app(admin_emails: list[str]) -> FastAPI:
        app = FastAPI()
        app.add_middleware(
            ActiveUserGateMiddleware,
            users=users,
            sessions=sessions,
            clock=FakeClock(),
            templates=templates,
            admin_emails=admin_emails,
        )
        app.include_router(
            build_admin_router(
                templates=templates, users=users, sessions=sessions, status_changer=status_changer
            )
        )
        return app

    admin_client = TestClient(_app([user.email]))
    admin_client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    assert admin_client.get("/admin").status_code == 200

    demoted_client = TestClient(_app([]))
    demoted_client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    assert demoted_client.get("/admin").status_code == 403
