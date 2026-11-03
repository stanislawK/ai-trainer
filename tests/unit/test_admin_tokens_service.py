"""Issuing, listing and revoking admin tokens (ADR-0036, #455), on an in-memory repository."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_trainer.application.admin_tokens import AdminTokens
from ai_trainer.domain.admin_tokens import (
    MAX_TOKEN_DAYS,
    TOKEN_PREFIX,
    AdminToken,
    InvalidTokenLifetimeError,
    TokenState,
    token_hash,
)
from tests.admin_tokens_support import MemoryAdminTokens

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
RANDOM = bytes(range(32))


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


def _service() -> tuple[AdminTokens, MemoryAdminTokens, Clock]:
    repository, clock = MemoryAdminTokens(), Clock()
    return (
        AdminTokens(tokens=repository, clock=clock, random_bytes=lambda n: RANDOM[:n]),
        (repository),
        clock,
    )


async def test_issue_stores_only_the_hash_and_prefix() -> None:
    service, repository, _ = _service()
    user_id = uuid4()

    issued = await service.issue(user_id, days=7)

    encoded = base64.urlsafe_b64encode(RANDOM).rstrip(b"=").decode()
    assert issued.secret == f"{TOKEN_PREFIX}{encoded}"
    [row] = repository.rows.values()
    assert row.token_hash == hashlib.sha256(issued.secret.encode()).hexdigest()
    assert row.display_prefix == issued.secret[:12]
    assert issued.secret not in row.model_dump_json()
    assert issued.token.display_prefix == issued.secret[:12]
    assert "token_hash" not in AdminToken.model_fields


async def test_issue_draws_32_random_bytes() -> None:
    asked: list[int] = []

    def random_bytes(n: int) -> bytes:
        asked.append(n)
        return RANDOM[:n]

    service = AdminTokens(tokens=MemoryAdminTokens(), clock=Clock(), random_bytes=random_bytes)

    await service.issue(uuid4(), days=1)

    assert asked == [32]


async def test_issue_accepts_30_days() -> None:
    service, _, _ = _service()

    issued = await service.issue(uuid4(), days=MAX_TOKEN_DAYS)

    assert issued.token.expires_at == NOW + timedelta(days=30)
    assert issued.token.created_at == NOW


@pytest.mark.parametrize("days", [31, 0, -1, 365], ids=["31", "0", "-1", "365"])
async def test_issue_refuses_more_than_30_days(days: int) -> None:
    service, repository, _ = _service()

    with pytest.raises(InvalidTokenLifetimeError):
        await service.issue(uuid4(), days=days)

    assert repository.rows == {}


async def test_list_shows_only_the_admins_own_tokens_newest_first() -> None:
    service, _, clock = _service()
    me, other = uuid4(), uuid4()
    first = await service.issue(me, days=1)
    clock.at = NOW + timedelta(hours=1)
    second = await service.issue(me, days=1)
    await service.issue(other, days=1)

    listed = await service.list(me)

    assert [token.id for token in listed] == [second.token.id, first.token.id]


async def test_revoke_marks_an_active_token_revoked() -> None:
    service, _, clock = _service()
    me = uuid4()
    issued = await service.issue(me, days=1)
    clock.at = NOW + timedelta(minutes=5)

    revoked = await service.revoke(me, issued.token.id)

    assert revoked is not None
    assert revoked.revoked_at == NOW + timedelta(minutes=5)
    assert revoked.state_at(clock.at) is TokenState.REVOKED


async def test_revoking_another_users_token_finds_nothing() -> None:
    service, repository, _ = _service()
    issued = await service.issue(uuid4(), days=1)

    assert await service.revoke(uuid4(), issued.token.id) is None
    assert repository.revoked == {}


async def test_revoking_an_expired_token_changes_nothing() -> None:
    service, repository, clock = _service()
    me = uuid4()
    issued = await service.issue(me, days=1)
    clock.at = NOW + timedelta(days=2)

    unchanged = await service.revoke(me, issued.token.id)

    assert unchanged is not None
    assert unchanged.state_at(clock.at) is TokenState.EXPIRED
    assert repository.revoked == {}


def test_token_hash_is_the_hex_sha256_of_the_token() -> None:
    assert token_hash("atk_abc") == hashlib.sha256(b"atk_abc").hexdigest()


@pytest.mark.parametrize(
    ("at", "revoked_at", "state"),
    [
        (NOW, None, TokenState.ACTIVE),
        (NOW + timedelta(days=1) - timedelta(microseconds=1), None, TokenState.ACTIVE),
        (NOW + timedelta(days=1), None, TokenState.EXPIRED),
        (NOW, NOW, TokenState.REVOKED),
        (NOW + timedelta(days=2), NOW, TokenState.REVOKED),
    ],
    ids=["fresh", "last-instant", "at-expiry", "revoked", "revoked-and-expired"],
)
def test_a_tokens_state(at: datetime, revoked_at: datetime | None, state: TokenState) -> None:
    token = AdminToken(
        id=uuid4(),
        user_id=uuid4(),
        display_prefix="atk_AAECAwQF",
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
        revoked_at=revoked_at,
    )

    assert token.state_at(at) is state


def test_the_service_reads_its_time_from_the_clock() -> None:
    # The service's view of "now", for the list's state badges (ADR-0014).
    service, _, clock = _service()

    assert service.now() == clock.at


async def test_an_issued_tokens_repr_hides_the_secret() -> None:
    # So a log line or a traceback that prints it never carries the token (ADR-0030).
    service, _, _ = _service()

    issued = await service.issue(uuid4(), days=1)

    assert issued.secret not in repr(issued)
    assert "<hidden>" in repr(issued)
