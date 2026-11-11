"""The admin an operator command runs as (ADR-0031, #533, #555): an email listed in
`ADMIN_EMAILS` (ADR-0005) that an active account has. Its user ID is the `user_id` of the run's
`llm_calls` rows, so no unverified `user_id` is ever written."""

from collections.abc import Sequence
from uuid import UUID

from ai_trainer.application.ports.users import ActiveUsersByEmailPort
from ai_trainer.domain.users import is_admin_email


async def resolve_active_admin(
    email: str, *, admin_emails: Sequence[str], users: ActiveUsersByEmailPort
) -> UUID | None:
    """The admin's user ID, or `None` when the email isn't listed or has no active account."""
    if not is_admin_email(email, admin_emails):
        return None
    user = await users.active_by_email(email)
    return None if user is None else user.id


def default_admin_email(admin_email: str | None, admin_emails: Sequence[str]) -> str | None:
    """The email a command names, else the one `ADMIN_EMAILS` entry when there is exactly one."""
    if admin_email:
        return admin_email
    return admin_emails[0] if len(admin_emails) == 1 else None
