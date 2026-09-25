from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class UserStatus(StrEnum):
    """Access is approval-gated (ADR-0005): only `ACTIVE` may use the app."""

    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class GoogleClaims(BaseModel):
    """Validated OpenID Connect claims from Google. No access or refresh token (ADR-0005)."""

    sub: str
    email: str
    email_verified: bool
    name: str | None = None
    locale: str | None = None


def is_admin_email(email: str, admin_emails: Sequence[str]) -> bool:
    """Whether `email` is in `ADMIN_EMAILS`, case-insensitively (ADR-0005: admin rights are
    derived per request, never stored as a database role)."""
    admin_emails_casefold = {address.casefold() for address in admin_emails}
    return email.casefold() in admin_emails_casefold


def resolve_initial_status(
    email: str, email_verified: bool, admin_emails: Sequence[str]
) -> UserStatus:
    """A new account is `active` only when its verified email is in `ADMIN_EMAILS`."""
    if email_verified and is_admin_email(email, admin_emails):
        return UserStatus.ACTIVE
    return UserStatus.PENDING


class NewUser(BaseModel):
    """A user about to be persisted; the repository assigns `id` and `created_at`."""

    sub: str
    email: str
    name: str | None
    locale: str
    status: UserStatus


class User(NewUser):
    """A persisted `users` row."""

    id: UUID
    created_at: datetime


class UserAlreadyExistsError(Exception):
    """Raised when a `create` loses a race: another sign-in already took this `sub`."""

    def __init__(self, sub: str) -> None:
        super().__init__(f"a user with sub {sub!r} already exists")
        self.sub = sub


class CannotChangeOwnStatusError(Exception):
    """Raised when an admin tries to change their own status (ADR-0005 invariant: an admin
    cannot change their own status)."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(f"user {user_id} cannot change their own status")
        self.user_id = user_id


class UserNotFoundError(Exception):
    """Raised when a status change targets a user that no longer exists."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(f"user {user_id} not found")
        self.user_id = user_id
