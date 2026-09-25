from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from ai_trainer.domain.users import UserStatus


class NewUserStatusChange(BaseModel):
    """A status change about to be persisted; the repository assigns `id` and `created_at`
    (ADR-0005 invariant 9: every status change records actor, target, old status, new status
    and timestamp)."""

    actor_user_id: UUID
    target_user_id: UUID
    old_status: UserStatus
    new_status: UserStatus


class UserStatusChange(NewUserStatusChange):
    """A persisted `user_status_changes` row."""

    id: UUID
    created_at: datetime
