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
from scripts.latency_scenario import DEV_LATENCY_SUB, Turn, main


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


@pytest.mark.usefixtures("_cleanup_latency_athlete")
async def test_a_gym_run_taps_each_question_on_the_card_then_confirms(tmp_path: Path) -> None:
    """No model is called: the app is a stub whose card asks twice, and a confirm before both
    answers would be refused, as the real one is."""
    draft = "11111111-1111-1111-1111-111111111111"
    questions = ["22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333"]
    message = "44444444-4444-4444-4444-444444444444"
    answered: list[tuple[str, str]] = []

    def card(question: str | None) -> str:
        options = (
            f"""<button hx-post="/clarifications/{question}/answer" """
            f"""hx-vals='{{"option_ids": "pick-{question}"}}' data-choice-option="x"></button>"""
            if question
            else ""
        )
        return f'<article data-draft-id="{draft}">{options}</article>'

    def app(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/health":
            return httpx2.Response(200, json={"database": "ok"})
        if path == "/messages":
            return httpx2.Response(200, text=f'<div hx-sse:connect="/messages/{message}/reply">')
        if path == f"/messages/{message}/reply":
            event = f'data: <template hx-target="#reply-{message}">{card(questions[0])}</template>'
            return httpx2.Response(200, text=f"{event}\n\n")
        if path == f"/drafts/{draft}/confirm":
            return httpx2.Response(409 if len(answered) < len(questions) else 200, text="")
        question = path.split("/")[2]
        answered.append((question, request.content.decode()))
        return httpx2.Response(200, text=card(questions[1] if len(answered) == 1 else None))

    output = io.StringIO()

    code = await main(
        base_url="http://testserver",
        output=output,
        settings=Settings(llm_debug_log_dir=tmp_path),
        transport=httpx2.MockTransport(app),
        turns=(Turn("Bench press 3x8 60kg", True, True),),
        sport="gym",
    )

    assert code == 0
    assert "failures 0" in output.getvalue()
    assert [question for question, _ in answered] == questions
    assert answered[0][1] == f"option_ids=pick-{questions[0]}"
    [written] = tmp_path.glob("latency-scenario-*.json")
    assert json.loads(written.read_text())["turns"][0]["outcome"] == "ok"


@pytest.mark.usefixtures("_cleanup_latency_athlete")
async def test_a_refused_tap_names_the_turn_as_failed(tmp_path: Path) -> None:
    draft = "11111111-1111-1111-1111-111111111111"
    question = "22222222-2222-2222-2222-222222222222"
    message = "44444444-4444-4444-4444-444444444444"

    def app(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/health":
            return httpx2.Response(200, json={"database": "ok"})
        if path == "/messages":
            return httpx2.Response(200, text=f'<div hx-sse:connect="/messages/{message}/reply">')
        if path == f"/messages/{message}/reply":
            event = (
                f'data: <template hx-target="#reply-{message}"><article data-draft-id="{draft}">'
                f"""<button hx-post="/clarifications/{question}/answer" """
                """hx-vals='{"option_ids": "keep"}'></button></article></template>"""
            )
            return httpx2.Response(200, text=f"{event}\n\n")
        return httpx2.Response(422, text="")

    output = io.StringIO()

    await main(
        base_url="http://testserver",
        output=output,
        settings=Settings(llm_debug_log_dir=tmp_path),
        transport=httpx2.MockTransport(app),
        turns=(Turn("Bench press", True, True),),
        sport="gym",
    )

    assert "confirm_http_422" in output.getvalue()
