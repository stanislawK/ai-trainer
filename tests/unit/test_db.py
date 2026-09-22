from sqlalchemy.ext.asyncio import AsyncEngine

from ai_trainer.adapters.db import build_engine, build_session_factory


def test_build_engine_returns_an_async_engine_bound_to_the_given_url() -> None:
    engine = build_engine("postgresql+psycopg://ai_trainer:ai_trainer@localhost:5432/ai_trainer")

    assert isinstance(engine, AsyncEngine)
    assert engine.sync_engine.url.drivername == "postgresql+psycopg"


def test_build_session_factory_disables_expire_on_commit() -> None:
    engine = build_engine("postgresql+psycopg://ai_trainer:ai_trainer@localhost:5432/ai_trainer")

    session_factory = build_session_factory(engine)

    session = session_factory()

    assert session.sync_session.expire_on_commit is False
