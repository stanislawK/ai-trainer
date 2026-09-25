from datetime import UTC, datetime
from uuid import uuid4

from ai_trainer.domain.user_status_changes import NewUserStatusChange, UserStatusChange
from ai_trainer.domain.users import UserStatus

FROZEN_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def test_new_user_status_change_carries_actor_target_and_both_statuses() -> None:
    actor_id = uuid4()
    target_id = uuid4()

    change = NewUserStatusChange(
        actor_user_id=actor_id,
        target_user_id=target_id,
        old_status=UserStatus.PENDING,
        new_status=UserStatus.ACTIVE,
    )

    assert change.actor_user_id == actor_id
    assert change.target_user_id == target_id
    assert change.old_status is UserStatus.PENDING
    assert change.new_status is UserStatus.ACTIVE


def test_persisted_user_status_change_adds_id_and_created_at() -> None:
    change = UserStatusChange(
        id=uuid4(),
        actor_user_id=uuid4(),
        target_user_id=uuid4(),
        old_status=UserStatus.ACTIVE,
        new_status=UserStatus.DISABLED,
        created_at=FROZEN_NOW,
    )

    assert change.created_at == FROZEN_NOW
