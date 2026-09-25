from uuid import UUID

from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.user_status_changer import UserStatusChangerPort
from ai_trainer.domain.users import CannotChangeOwnStatusError, User, UserStatus


async def change_user_status(
    actor: User,
    target_user_id: UUID,
    new_status: UserStatus,
    *,
    status_changer: UserStatusChangerPort,
    sessions: SessionsRepositoryPort,
) -> tuple[User, UserStatus]:
    """Moves `target_user_id` to `new_status` (F7, ADR-0005) and returns the updated user
    alongside their previous status (so a caller can offer to reverse the change). Raises
    `CannotChangeOwnStatusError` when the actor targets themselves, and `UserNotFoundError`
    when the target doesn't exist. The status update and its audit record commit atomically
    (invariant 9, `UserStatusChangerPort`); sessions are revoked immediately afterward when the
    target leaves `active` (invariant 10) — a real, if vanishingly unlikely, gap between these
    two steps is harmless because `ActiveUserGateMiddleware` re-checks the user's status fresh
    on every request regardless of whether their session row still exists."""
    if actor.id == target_user_id:
        raise CannotChangeOwnStatusError(actor.id)

    updated, previous_status = await status_changer.change(
        target_user_id=target_user_id, new_status=new_status, actor_user_id=actor.id
    )

    if new_status is not UserStatus.ACTIVE:
        await sessions.delete_for_user(target_user_id)

    return updated, previous_status
