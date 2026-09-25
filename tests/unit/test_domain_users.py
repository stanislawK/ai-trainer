from uuid import uuid4

import pytest

from ai_trainer.domain.users import (
    CannotChangeOwnStatusError,
    GoogleClaims,
    UserNotFoundError,
    UserStatus,
    is_admin_email,
    resolve_initial_status,
)


def test_unverified_admin_email_is_still_pending() -> None:
    status = resolve_initial_status(
        email="admin@example.com", email_verified=False, admin_emails=["admin@example.com"]
    )

    assert status is UserStatus.PENDING


def test_verified_admin_email_is_active() -> None:
    status = resolve_initial_status(
        email="admin@example.com", email_verified=True, admin_emails=["admin@example.com"]
    )

    assert status is UserStatus.ACTIVE


def test_admin_email_match_is_case_insensitive() -> None:
    status = resolve_initial_status(
        email="Admin@Example.com", email_verified=True, admin_emails=["admin@example.com"]
    )

    assert status is UserStatus.ACTIVE


def test_verified_non_admin_email_is_pending() -> None:
    status = resolve_initial_status(
        email="athlete@example.com", email_verified=True, admin_emails=["admin@example.com"]
    )

    assert status is UserStatus.PENDING


def test_is_admin_email_matches_a_listed_address() -> None:
    assert is_admin_email("admin@example.com", ["admin@example.com"]) is True


def test_is_admin_email_is_case_insensitive() -> None:
    assert is_admin_email("Admin@Example.com", ["admin@example.com"]) is True


def test_is_admin_email_rejects_an_unlisted_address() -> None:
    assert is_admin_email("athlete@example.com", ["admin@example.com"]) is False


def test_is_admin_email_rejects_everything_against_an_empty_list() -> None:
    assert is_admin_email("admin@example.com", []) is False


def test_google_claims_carries_no_token_fields() -> None:
    claims = GoogleClaims(sub="123", email="a@example.com", email_verified=True)

    assert set(GoogleClaims.model_fields) == {"sub", "email", "email_verified", "name", "locale"}
    assert claims.name is None
    assert claims.locale is None


def test_cannot_change_own_status_error_carries_the_user_id() -> None:
    user_id = uuid4()

    error = CannotChangeOwnStatusError(user_id)

    assert error.user_id == user_id
    assert str(user_id) in str(error)


def test_user_not_found_error_carries_the_user_id() -> None:
    user_id = uuid4()

    error = UserNotFoundError(user_id)

    assert error.user_id == user_id
    assert str(user_id) in str(error)


def test_errors_are_raisable_with_pytest_raises() -> None:
    with pytest.raises(CannotChangeOwnStatusError):
        raise CannotChangeOwnStatusError(uuid4())
    with pytest.raises(UserNotFoundError):
        raise UserNotFoundError(uuid4())
