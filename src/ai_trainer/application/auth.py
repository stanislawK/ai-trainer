from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import (
    GoogleClaims,
    NewUser,
    User,
    UserAlreadyExistsError,
    resolve_initial_status,
)


async def sign_in_with_google(
    claims: GoogleClaims,
    *,
    admin_emails: Sequence[str],
    session_ttl: timedelta,
    users: UsersRepositoryPort,
    sessions: SessionsRepositoryPort,
    clock: ClockPort,
) -> tuple[User, Session]:
    """Gets or creates the user by `sub`, then always opens a new session (ADR-0005)."""
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
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=clock.now() + session_ttl)
    )
    return user, session


async def sign_out(session_id: UUID, sessions: SessionsRepositoryPort) -> None:
    await sessions.delete(session_id)
