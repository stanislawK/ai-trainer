"""Unit tests for `change_user_status` (ADR-0005 invariants 6, 9, 10): the one application use
case the admin status-change route calls."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.admin import change_user_status
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import CannotChangeOwnStatusError, User, UserNotFoundError, UserStatus

FROZEN_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


class FakeUserStatusChanger:
    """Fakes the atomic status-update-plus-audit-write (ADR-0005 invariant 9)."""

    def __init__(self, *users: User) -> None:
        self._users = {user.id: user for user in users}
        self.recorded: list[tuple[UUID, UUID, UserStatus, UserStatus]] = []

    async def change(
        self, *, target_user_id: UUID, new_status: UserStatus, actor_user_id: UUID
    ) -> tuple[User, UserStatus]:
        if target_user_id not in self._users:
            raise UserNotFoundError(target_user_id)
        target = self._users[target_user_id]
        old_status = target.status
        updated = target.model_copy(update={"status": new_status})
        self._users[target_user_id] = updated
        self.recorded.append((actor_user_id, target_user_id, old_status, new_status))
        return updated, old_status


class FakeSessionsRepository:
    def __init__(self) -> None:
        self.deleted_for_user: list[UUID] = []

    async def create(self, new_session: NewSession) -> Session:
        raise NotImplementedError

    async def get(self, session_id: UUID) -> Session | None:
        raise NotImplementedError

    async def delete(self, session_id: UUID) -> None:
        raise NotImplementedError

    async def delete_for_user(self, user_id: UUID) -> None:
        self.deleted_for_user.append(user_id)


def _user(status: UserStatus = UserStatus.PENDING) -> User:
    return User(
        id=uuid4(),
        sub="google-sub-1",
        email="athlete@example.com",
        name="Athlete",
        locale="en",
        status=status,
        created_at=FROZEN_NOW,
    )


def _admin() -> User:
    return User(
        id=uuid4(),
        sub="google-sub-admin",
        email="admin@example.com",
        name="Admin",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=FROZEN_NOW,
    )


async def test_activating_a_pending_user_updates_their_status() -> None:
    admin = _admin()
    target = _user(UserStatus.PENDING)
    status_changer = FakeUserStatusChanger(admin, target)
    sessions = FakeSessionsRepository()

    updated, previous_status = await change_user_status(
        admin, target.id, UserStatus.ACTIVE, status_changer=status_changer, sessions=sessions
    )

    assert updated.status is UserStatus.ACTIVE
    assert previous_status is UserStatus.PENDING


async def test_activating_a_user_does_not_revoke_any_session() -> None:
    admin = _admin()
    target = _user(UserStatus.PENDING)
    status_changer = FakeUserStatusChanger(admin, target)
    sessions = FakeSessionsRepository()

    await change_user_status(
        admin, target.id, UserStatus.ACTIVE, status_changer=status_changer, sessions=sessions
    )

    assert sessions.deleted_for_user == []


async def test_disabling_an_active_user_revokes_their_sessions_immediately() -> None:
    admin = _admin()
    target = _user(UserStatus.ACTIVE)
    status_changer = FakeUserStatusChanger(admin, target)
    sessions = FakeSessionsRepository()

    await change_user_status(
        admin, target.id, UserStatus.DISABLED, status_changer=status_changer, sessions=sessions
    )

    assert sessions.deleted_for_user == [target.id]


async def test_moving_a_user_to_pending_also_revokes_their_sessions() -> None:
    admin = _admin()
    target = _user(UserStatus.ACTIVE)
    status_changer = FakeUserStatusChanger(admin, target)
    sessions = FakeSessionsRepository()

    await change_user_status(
        admin, target.id, UserStatus.PENDING, status_changer=status_changer, sessions=sessions
    )

    assert sessions.deleted_for_user == [target.id]


async def test_the_status_update_and_its_audit_record_happen_atomically() -> None:
    """`change_user_status` delegates to a single `UserStatusChangerPort.change` call rather
    than separate update-then-record steps, so the two can never diverge (ADR-0005 invariant 9,
    ticket #16 review-gate skeptic finding)."""
    admin = _admin()
    target = _user(UserStatus.PENDING)
    status_changer = FakeUserStatusChanger(admin, target)
    sessions = FakeSessionsRepository()

    await change_user_status(
        admin, target.id, UserStatus.ACTIVE, status_changer=status_changer, sessions=sessions
    )

    assert len(status_changer.recorded) == 1
    actor_id, target_id, old_status, new_status = status_changer.recorded[0]
    assert actor_id == admin.id
    assert target_id == target.id
    assert old_status is UserStatus.PENDING
    assert new_status is UserStatus.ACTIVE


async def test_admin_cannot_change_their_own_status() -> None:
    admin = _admin()
    status_changer = FakeUserStatusChanger(admin)
    sessions = FakeSessionsRepository()

    with pytest.raises(CannotChangeOwnStatusError):
        await change_user_status(
            admin, admin.id, UserStatus.DISABLED, status_changer=status_changer, sessions=sessions
        )

    assert sessions.deleted_for_user == []
    assert status_changer.recorded == []


async def test_changing_a_missing_user_raises_user_not_found() -> None:
    admin = _admin()
    status_changer = FakeUserStatusChanger(admin)
    sessions = FakeSessionsRepository()

    with pytest.raises(UserNotFoundError):
        await change_user_status(
            admin, uuid4(), UserStatus.ACTIVE, status_changer=status_changer, sessions=sessions
        )
