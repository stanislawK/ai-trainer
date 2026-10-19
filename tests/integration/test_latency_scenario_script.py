"""`scripts/latency_scenario.py`'s `main()` seeds its athlete in the real compose Postgres, like
`dev_session.py` does, so this cleans up after itself rather than relying on rollback. The app
is a stub that answers `/health` only: no turn is played, so no model is called."""

import io
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import text

from ai_trainer.adapters.db import build_engine
from ai_trainer.settings import Settings
from scripts.latency_scenario import DEV_LATENCY_SUB, main


@pytest.fixture
async def _cleanup_latency_athlete() -> AsyncIterator[None]:
    yield
    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM users WHERE sub = :sub"), {"sub": DEV_LATENCY_SUB}
        )
    await engine.dispose()


@pytest.mark.usefixtures("_cleanup_latency_athlete")
async def test_main_seeds_a_climbing_only_athlete_and_writes_the_summary(tmp_path: Path) -> None:
    def healthy(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/health"
        return httpx2.Response(200, json={"database": "ok"})

    output = io.StringIO()

    code = await main(
        base_url="http://testserver",
        output=output,
        settings=Settings(llm_debug_log_dir=tmp_path),
        transport=httpx2.MockTransport(healthy),
        turns=(),
    )

    assert code == 0
    assert "Log turns: 0" in output.getvalue()
    [written] = tmp_path.glob("latency-scenario-*.json")
    assert json.loads(written.read_text())["turns"] == []
    engine = build_engine(str(Settings().database_url))
    async with engine.connect() as connection:
        sports = await connection.scalars(
            text(
                "SELECT us.sport_id FROM user_sports us JOIN users u ON u.id = us.user_id"
                " WHERE u.sub = :sub AND u.onboarded_at IS NOT NULL"
            ),
            {"sub": DEV_LATENCY_SUB},
        )
        assert list(sports) == ["climbing"]
    await engine.dispose()
