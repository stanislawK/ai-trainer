"""`scripts/dev_session.py`'s `main()` writes real, non-transactional rows on purpose — that
is the whole point of a dev-session seeder — so this runs against the real compose Postgres
like `test_migrations.py` does, and cleans up after itself rather than relying on rollback."""

import io
import json
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy import text

from ai_trainer.adapters.db import build_engine
from ai_trainer.settings import Settings
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from scripts.dev_session import DEV_FRESH_SUB, DEV_SESSION_EMAIL, DEV_SESSION_SUB, main


@pytest.fixture
async def _cleanup_dev_session_rows() -> AsyncIterator[None]:
    yield
    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE sub = :sub)"),
            {"sub": DEV_SESSION_SUB},
        )
        await connection.execute(
            text("DELETE FROM users WHERE sub = :sub"), {"sub": DEV_SESSION_SUB}
        )
    await engine.dispose()


@pytest.mark.usefixtures("_cleanup_dev_session_rows")
async def test_main_prints_a_cookie_for_a_freshly_seeded_session() -> None:
    output = io.StringIO()

    await main(output=output)

    cookie = json.loads(output.getvalue())
    assert cookie["name"] == SESSION_COOKIE_NAME
    UUID(cookie["value"])  # a real session id


@pytest.mark.usefixtures("_cleanup_dev_session_rows")
async def test_main_reuses_the_seeded_user_but_opens_a_new_session_each_run() -> None:
    first_output = io.StringIO()
    second_output = io.StringIO()

    await main(output=first_output)
    await main(output=second_output)

    first_cookie = json.loads(first_output.getvalue())
    second_cookie = json.loads(second_output.getvalue())
    assert first_cookie["value"] != second_cookie["value"]

    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        result = await connection.execute(
            text("SELECT count(*) FROM users WHERE sub = :sub"), {"sub": DEV_SESSION_SUB}
        )
        assert result.scalar_one() == 1
    await engine.dispose()


@pytest.mark.usefixtures("_cleanup_dev_session_rows")
async def test_main_seeds_the_dev_session_user_as_active() -> None:
    output = io.StringIO()

    await main(output=output)

    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        result = await connection.execute(
            text("SELECT status, email FROM users WHERE sub = :sub"), {"sub": DEV_SESSION_SUB}
        )
        status, email = result.one()
        assert status == "active"
        assert email == DEV_SESSION_EMAIL
    await engine.dispose()


@pytest.mark.usefixtures("_cleanup_dev_session_rows")
async def test_main_seeds_the_default_user_as_onboarded() -> None:
    await main(output=io.StringIO())

    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        result = await connection.execute(
            text("SELECT onboarded_at IS NOT NULL FROM users WHERE sub = :sub"),
            {"sub": DEV_SESSION_SUB},
        )
        assert result.scalar_one() is True
    await engine.dispose()


@pytest.fixture
async def _cleanup_fresh_dev_session_rows() -> AsyncIterator[None]:
    yield
    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM users WHERE sub = :sub"), {"sub": DEV_FRESH_SUB})
    await engine.dispose()


@pytest.mark.usefixtures("_cleanup_fresh_dev_session_rows")
async def test_main_with_not_onboarded_seeds_a_user_without_onboarded_at() -> None:
    await main(output=io.StringIO(), not_onboarded=True)

    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        result = await connection.execute(
            text("SELECT onboarded_at IS NULL FROM users WHERE sub = :sub"),
            {"sub": DEV_FRESH_SUB},
        )
        assert result.scalar_one() is True
    await engine.dispose()
