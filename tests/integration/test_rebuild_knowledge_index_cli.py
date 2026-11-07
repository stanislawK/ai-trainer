"""`ai-trainer rebuild-knowledge-index` (ADR-0036's #462 amendment): after a bulk load the HNSW
index is rebuilt with a raised `maintenance_work_mem`, and the connection gets its own back."""

import io
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ai_trainer import cli, cli_knowledge
from ai_trainer.adapters.knowledge_index import HNSW_INDEX, PostgresKnowledgeIndex
from ai_trainer.settings import Settings


@pytest.fixture
async def one_connection() -> AsyncIterator[AsyncEngine]:
    """A pool of one connection, so the next checkout is the connection the rebuild used."""
    engine = create_async_engine(str(Settings().database_url), pool_size=1, max_overflow=0)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _read(engine: AsyncEngine, sql: str, **params: str) -> str:
    async with engine.connect() as conn:
        value = (await conn.execute(text(sql), params)).scalar_one()
    return str(value)


async def _setting(engine: AsyncEngine) -> str:
    return await _read(engine, "SHOW maintenance_work_mem")


async def _file(engine: AsyncEngine, index: str) -> str:
    return await _read(engine, "SELECT relfilenode FROM pg_class WHERE relname = :name", name=index)


async def test_rebuild_recreates_the_hnsw_index_and_restores_maintenance_work_mem(
    one_connection: AsyncEngine,
) -> None:
    before_setting = await _setting(one_connection)
    before_file = await _file(one_connection, HNSW_INDEX)

    used = await PostgresKnowledgeIndex(one_connection).rebuild(memory_mb=128)

    assert used == "128MB"
    assert await _file(one_connection, HNSW_INDEX) != before_file
    assert await _setting(one_connection) == before_setting
    definition = await _read(
        one_connection, "SELECT indexdef FROM pg_indexes WHERE indexname = :name", name=HNSW_INDEX
    )
    assert "USING hnsw (embedding vector_cosine_ops)" in definition


async def test_a_failed_rebuild_still_restores_maintenance_work_mem(
    one_connection: AsyncEngine,
) -> None:
    before = await _setting(one_connection)

    with pytest.raises(Exception, match="no_such_index"):
        await PostgresKnowledgeIndex(one_connection, index="no_such_index").rebuild(memory_mb=128)

    assert await _setting(one_connection) == before


async def test_the_command_prints_one_line() -> None:
    output = io.StringIO()

    code = await cli.run(
        [cli_knowledge.REBUILD_KNOWLEDGE_INDEX, "--memory-mb", "96"], output=output
    )

    assert code == 0
    assert output.getvalue() == (
        f"rebuild-knowledge-index: result=ok index={HNSW_INDEX} maintenance_work_mem=96MB\n"
    )
