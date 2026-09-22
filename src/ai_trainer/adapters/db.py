from pgvector.psycopg import register_vector_async
from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry


def build_engine(database_url: str) -> AsyncEngine:
    """Async engine on psycopg 3 (ADR-0004); registers pgvector codecs on every connection."""
    engine = create_async_engine(database_url)

    @event.listens_for(engine.sync_engine, "connect")
    def _register_vector_codecs(
        dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
    ) -> None:
        dbapi_connection.run_async(register_vector_async)

    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
