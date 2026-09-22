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


def resolve_initial_status(
    email: str, email_verified: bool, admin_emails: Sequence[str]
) -> UserStatus:
    """A new account is `active` only when its verified email is in `ADMIN_EMAILS`."""
    admin_emails_casefold = {address.casefold() for address in admin_emails}
    if email_verified and email.casefold() in admin_emails_casefold:
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
