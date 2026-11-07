"""The chunks' HNSW index rebuild on PostgreSQL (ADR-0036's #462 amendment).

pgvector builds an index much faster when its graph fits in `maintenance_work_mem`, which is 64 MB
by default. After a bulk load, `REINDEX INDEX CONCURRENTLY` rebuilds it with that raised for one
connection, so search keeps answering from the old index until the new one is swapped in. The
setting is a session's, so it is reset before the connection goes back to the pool.

Measured on 100,000 chunks (2026-11-06): the index is 781 MB, and at 256 MB its graph stops
fitting after about 55,000 of them. A parallel build allocates the setting from the db container's
`shm_size` (256 MiB), which caps it there.

A failed concurrent rebuild leaves an invalid `<index>_ccnew` index behind, which
`deploy/RUNBOOK.md` says how to drop.
"""

import contextlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

HNSW_INDEX = "ix_knowledge_chunks_embedding"


class PostgresKnowledgeIndex:
    """Implements `KnowledgeIndexPort`."""

    def __init__(self, engine: AsyncEngine, *, index: str = HNSW_INDEX) -> None:
        self._engine = engine
        self._index = index

    @property
    def index(self) -> str:
        return self._index

    async def rebuild(self, *, memory_mb: int) -> str:
        async with self._engine.connect() as connection:
            # REINDEX … CONCURRENTLY can't run inside a transaction block.
            conn = await connection.execution_options(isolation_level="AUTOCOMMIT")
            used = (
                await conn.execute(
                    text("SELECT set_config('maintenance_work_mem', :value, false)"),
                    {"value": f"{int(memory_mb)}MB"},
                )
            ).scalar_one()
            # An identifier can't be a bound parameter, so the dialect quotes it.
            quoted = conn.dialect.identifier_preparer.quote(self._index)
            try:
                await conn.execute(text(f"REINDEX INDEX CONCURRENTLY {quoted}"))
            except Exception:
                # A rebuild that broke the connection would fail the RESET too, and that error
                # must not replace the rebuild's own (#462 review). A closed connection takes
                # its session setting with it.
                with contextlib.suppress(Exception):
                    await conn.execute(text("RESET maintenance_work_mem"))
                raise
            await conn.execute(text("RESET maintenance_work_mem"))
        return str(used)
