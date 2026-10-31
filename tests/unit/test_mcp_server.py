"""`query_sessions` through FastMCP's in-memory `Client(server)` (ADR-0010 invariant 4, ticket
#378): a happy path, tenancy, missing identity and domain failures, with no database."""

from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ai_trainer.application.training_history import SessionPage, TrainingHistory
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionSummary
from ai_trainer.mcp.identity import USER_ID_META_KEY
from ai_trainer.mcp.results import ToolResult
from ai_trainer.mcp.server import build_mcp_server
from tests.mcp_support import no_exercise_tools

_ALICE = uuid4()
_BOB = uuid4()


def _summary(day: date, notes: str | None = None) -> SessionSummary:
    return SessionSummary(
        id=uuid4(),
        sport_id="climbing",
        occurred_on=day,
        duration_min=90,
        rpe=8,
        feeling=None,
        notes=notes,
    )


_BOBS = _summary(date(2026, 10, 2), notes="Bob's private note")
_ALICES = _summary(date(2026, 10, 3))


class _FakeHistory:
    def __init__(self, rows: dict[UUID, list[SessionSummary]]) -> None:
        self._rows = rows

    async def list_sessions(
        self,
        user_id: UUID,
        *,
        sport_id: str | None,
        since: date | None,
        until: date | None,
        limit: int,
    ) -> Sequence[SessionSummary]:
        return self._rows.get(user_id, [])[:limit]


def _client(rows: dict[UUID, list[SessionSummary]] | None = None) -> Client[Any]:
    history = TrainingHistory(
        _FakeHistory(rows if rows is not None else {_ALICE: [_ALICES], _BOB: [_BOBS]}),
        default_sport_registry(),
    )
    return Client(build_mcp_server(history, *no_exercise_tools()))


def _meta(user_id: UUID) -> dict[str, Any]:
    return {USER_ID_META_KEY: str(user_id)}


async def _query(client: Client[Any], user_id: UUID, **args: Any) -> ToolResult[SessionPage]:
    async with client:
        result = await client.call_tool("query_sessions", args, meta=_meta(user_id))
    return ToolResult[SessionPage].model_validate(result.structured_content)


async def test_the_users_sessions_come_back_in_the_envelope() -> None:
    result = await _query(_client(), _ALICE)

    assert result == ToolResult[SessionPage](
        success=True, data=SessionPage(sessions=[_ALICES], truncated=False)
    )


async def test_alice_never_gets_bobs_sessions() -> None:
    result = await _query(_client({_BOB: [_BOBS]}), _ALICE)

    assert result.success is False
    assert str(_BOBS.id) not in result.model_dump_json()
    assert "Bob's private note" not in result.model_dump_json()


async def test_a_call_with_no_identity_raises_tool_error() -> None:
    async with _client() as client:
        with pytest.raises(ToolError):
            await client.call_tool("query_sessions", {})


@pytest.mark.parametrize(
    "meta",
    [{}, {USER_ID_META_KEY: None}, {USER_ID_META_KEY: "not-a-uuid"}, {"other": "x"}],
    ids=["empty", "none", "malformed", "other-key"],
)
async def test_a_call_with_unusable_identity_raises_tool_error(meta: dict[str, Any]) -> None:
    async with _client() as client:
        with pytest.raises(ToolError):
            await client.call_tool("query_sessions", {}, meta=meta)


async def test_an_unknown_sport_returns_a_recovery_hint_naming_the_sports() -> None:
    result = await _query(_client(), _ALICE, sport="curling")

    assert result.success is False
    assert result.data is None
    assert result.recovery_hint is not None
    for plugin in default_sport_registry().all():
        assert plugin.id in result.recovery_hint


async def test_since_after_until_returns_a_recovery_hint() -> None:
    result = await _query(_client(), _ALICE, since="2026-10-05", until="2026-10-01")

    assert result.success is False
    assert result.error is not None
    assert result.recovery_hint is not None


async def test_no_matching_sessions_returns_a_recovery_hint() -> None:
    result = await _query(_client({}), _ALICE)

    assert result.success is False
    assert result.recovery_hint is not None


async def test_schema_invalid_arguments_are_a_tool_error() -> None:
    async with _client() as client:
        result = await client.call_tool(
            "query_sessions", {"limit": 500}, meta=_meta(_ALICE), raise_on_error=False
        )

    assert result.is_error is True


async def test_every_tool_publishes_a_described_schema_without_a_user_parameter() -> None:
    async with _client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == {"query_sessions", "find_exercise", "exercise_history"}
    expected = {
        "query_sessions": {"sport", "since", "until", "limit"},
        "find_exercise": {"name"},
        "exercise_history": {"exercise_id", "limit"},
    }
    for name, tool in tools.items():
        assert tool.description is not None
        assert tool.description.count(". ") >= 3, name
        assert set(tool.input_schema["properties"]) == expected[name], name
        assert tool.output_schema is not None, name
    assert tools["query_sessions"].input_schema["properties"]["limit"]["maximum"] == 50
    assert tools["exercise_history"].input_schema["properties"]["limit"]["maximum"] == 50


def test_tool_result_constructors() -> None:
    page = SessionPage(sessions=[], truncated=False)

    assert ToolResult.ok(page) == ToolResult[SessionPage](success=True, data=page)
    assert ToolResult[SessionPage].failed("bad", "retry") == ToolResult[SessionPage](
        success=False, error="bad", recovery_hint="retry"
    )
