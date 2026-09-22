import psycopg
import pytest

from ai_trainer.adapters.health import PsycopgDatabaseHealth, to_psycopg_dsn
from ai_trainer.settings import Settings


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql+psycopg://user:pass@localhost:5432/db",
            "postgresql://user:pass@localhost:5432/db",
        ),
        (
            "postgresql+asyncpg://user:pass@localhost:5432/db",
            "postgresql://user:pass@localhost:5432/db",
        ),
        (
            "postgresql://user:pass@localhost:5432/db",
            "postgresql://user:pass@localhost:5432/db",
        ),
    ],
)
def test_to_psycopg_dsn_strips_any_driver_suffix(dsn: str, expected: str) -> None:
    assert to_psycopg_dsn(dsn) == expected


async def test_ping_returns_true_for_a_reachable_database() -> None:
    settings = Settings()
    adapter = PsycopgDatabaseHealth(str(settings.database_url))

    assert await adapter.ping() is True


async def test_ping_returns_false_instead_of_raising_when_database_is_unreachable() -> None:
    adapter = PsycopgDatabaseHealth(
        "postgresql://ai_trainer:ai_trainer@localhost:1/ai_trainer",
        connect_timeout=1,
    )

    assert await adapter.ping() is False


async def test_ping_returns_false_instead_of_raising_for_a_malformed_dsn() -> None:
    # A DSN using a different SQLAlchemy-style driver suffix than the one this
    # adapter is built for: normalized by `to_psycopg_dsn`, but if that
    # normalization ever regressed, psycopg would raise ProgrammingError here,
    # not OperationalError — ping() must not let either kind crash the route.
    adapter = PsycopgDatabaseHealth("postgresql://user:pass@localhost:1/db?foo")

    assert await adapter.ping() is False


async def test_vector_extension_is_available() -> None:
    settings = Settings()
    dsn = to_psycopg_dsn(str(settings.database_url))

    async with await psycopg.AsyncConnection.connect(dsn) as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        row = await cur.fetchone()

    assert row is not None
