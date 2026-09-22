from pgvector import Vector
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_EMBEDDING = [1.0, 2.0, 3.0]


async def test_vector_column_round_trips_through_async_session(db_session: AsyncSession) -> None:
    # Scoped to this test's own transaction (rolled back at teardown), so it
    # never conflicts with test_migrations.py dropping the `vector` extension.
    await db_session.execute(
        text("CREATE TABLE vector_round_trip_probe (id INTEGER PRIMARY KEY, embedding VECTOR(3))")
    )
    await db_session.execute(
        text("INSERT INTO vector_round_trip_probe (id, embedding) VALUES (1, :embedding)"),
        {"embedding": Vector(_EMBEDDING)},
    )

    result = await db_session.execute(
        text("SELECT embedding FROM vector_round_trip_probe WHERE id = 1")
    )
    embedding = result.scalar_one()

    assert embedding.to_list() == _EMBEDDING
