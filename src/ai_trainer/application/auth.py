from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.user_status_changer import UserStatusChangerPort
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import (
    GoogleClaims,
    NewUser,
    User,
    UserAlreadyExistsError,
    UserStatus,
    resolve_initial_status,
    should_activate_on_sign_in,
)


async def sign_in_with_google(
    claims: GoogleClaims,
    *,
    admin_emails: Sequence[str],
    session_ttl: timedelta,
    users: UsersRepositoryPort,
    sessions: SessionsRepositoryPort,
    status_changer: UserStatusChangerPort,
    clock: ClockPort,
) -> tuple[User, Session]:
    """Gets or creates the user by `sub`, then always opens a new session (ADR-0005). An
    existing `pending` or `disabled` account with a listed, verified email is activated first,
    recorded with that admin as the actor (invariants 6 and 9)."""
    user = await users.get_by_sub(claims.sub)
    if user is None:
        status = resolve_initial_status(claims.email, claims.email_verified, admin_emails)
        try:
            user = await users.create(
                NewUser(
                    sub=claims.sub,
                    email=claims.email,
                    name=claims.name,
                    locale=claims.locale or "en",
                    status=status,
                )
            )
        except UserAlreadyExistsError:
            # Another concurrent sign-in for the same `sub` won the race; its row is now
            # visible, so fetch it instead of failing this request.
            user = await users.get_by_sub(claims.sub)
            if user is None:
                raise RuntimeError(
                    f"user {claims.sub!r} vanished after a concurrent create race"
                ) from None
    if should_activate_on_sign_in(user.status, claims.email, claims.email_verified, admin_emails):
        user, _ = await status_changer.change(
            target_user_id=user.id, new_status=UserStatus.ACTIVE, actor_user_id=user.id
        )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=clock.now() + session_ttl)
    )
    return user, session


async def sign_out(session_id: UUID, sessions: SessionsRepositoryPort) -> None:
    await sessions.delete(session_id)


async def resolve_authenticated_user(
    session_id: UUID,
    *,
    sessions: SessionsRepositoryPort,
    users: UsersRepositoryPort,
    clock: ClockPort,
) -> User | None:
    """Resolves the session's owner, fresh, so a status or session change takes effect on the
    very next request rather than only at sign-in (ADR-0005 invariant 7, ticket #14). Returns
    `None` for a missing, expired or already-cascaded-away session — never the caller's job to
    tell those apart, since all three mean "not authenticated"."""
    session = await sessions.get(session_id)
    if session is None or session.expires_at <= clock.now():
        return None
    return await users.get(session.user_id)
