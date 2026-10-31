"""`query_sessions` in process on PostgreSQL (ADR-0010, ticket #378): a Pydantic AI run reaches
the tool through the real `process_tool_call` hook with the user from deps, never sees another
user's sessions, stops before the call with no user, recovers from schema-invalid arguments,
and leaves no user ID or athlete text on any span (ADR-0018)."""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from fastmcp import Client
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import TrainingSessionOrm
from ai_trainer.adapters.telemetry import ScrubbingSpanExporter
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.training_history import TrainingHistory
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionSummary
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.mcp.identity import USER_ID_META_KEY, MissingUserError
from ai_trainer.mcp.server import build_mcp_server
from ai_trainer.mcp.toolset import build_toolset

_AT = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
_ALICE_NOTE = "Left ring finger sore after the crimpy boulder"
_BOB_NOTE = "Bob's knee clicked on the lunges"


@dataclass(frozen=True)
class _Deps:
    user_id: UUID | None


async def _user(factory: Callable[[], AsyncSession], sub: str) -> UUID:
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    return user.id


async def _session(
    factory: Callable[[], AsyncSession], user_id: UUID, day: date, notes: str, sport_id: str
) -> UUID:
    session_id = uuid4()
    row = TrainingSessionOrm(
        id=session_id,
        user_id=user_id,
        sport_id=sport_id,
        occurred_on=day,
        timezone="Europe/Warsaw",
        duration_min=75,
        rpe=7,
        feeling="tired",
        notes=notes,
        payload={"sport": sport_id, "activities": []},
        payload_version=1,
        created_at=_AT,
        updated_at=_AT,
    )
    async with factory() as db:
        db.add(row)
        await db.commit()
    return session_id


@dataclass(frozen=True)
class _World:
    alice: UUID
    bob: UUID
    alices_session: UUID
    bobs_session: UUID
    history: _SpyHistory
    history_use_case: TrainingHistory


class _SpyHistory:
    """The real repository, recording each read so a test can prove no read happened."""

    def __init__(self, inner: SqlAlchemyTrainingSessionRepository) -> None:
        self._inner = inner
        self.reads: list[UUID] = []

    async def list_sessions(
        self,
        user_id: UUID,
        *,
        sport_id: str | None,
        since: date | None,
        until: date | None,
        limit: int,
    ) -> Sequence[SessionSummary]:
        self.reads.append(user_id)
        return await self._inner.list_sessions(
            user_id, sport_id=sport_id, since=since, until=until, limit=limit
        )


@pytest.fixture
async def world(db_session_factory: Callable[[], AsyncSession]) -> _World:
    alice = await _user(db_session_factory, f"mcp-alice-{uuid4()}")
    bob = await _user(db_session_factory, f"mcp-bob-{uuid4()}")
    alices = await _session(db_session_factory, alice, date(2026, 10, 19), _ALICE_NOTE, "climbing")
    bobs = await _session(db_session_factory, bob, date(2026, 10, 20), _BOB_NOTE, "gym")
    spy = _SpyHistory(SqlAlchemyTrainingSessionRepository(db_session_factory))
    return _World(alice, bob, alices, bobs, spy, TrainingHistory(spy, default_sport_registry()))


def _agent(world: _World, model: TestModel | FunctionModel) -> Agent[_Deps, str]:
    toolset = build_toolset(build_mcp_server(world.history_use_case))
    return Agent(model, deps_type=_Deps, toolsets=[toolset])


def _tool_returns(messages: Sequence[ModelMessage]) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


# --- AC1: the user ID reaches the tool through meta ----------------------------------------


async def test_agent_run_reaches_tool_with_user_from_deps(world: _World) -> None:
    agent = _agent(world, TestModel(call_tools=["query_sessions"]))

    result = await agent.run("What did I train?", deps=_Deps(world.alice))

    assert world.history.reads == [world.alice]
    (returned,) = _tool_returns(result.all_messages())
    assert str(world.alices_session) in str(returned.content)
    assert str(world.bobs_session) not in str(returned.content)


# --- AC2: tenancy ---------------------------------------------------------------------------


async def test_user_a_never_sees_user_b_sessions(world: _World) -> None:
    server = build_mcp_server(world.history_use_case)

    async with Client(server) as client:
        alices = await client.call_tool(
            "query_sessions", {"limit": 50}, meta={USER_ID_META_KEY: str(world.alice)}
        )
        bobs = await client.call_tool(
            "query_sessions", {"limit": 50}, meta={USER_ID_META_KEY: str(world.bob)}
        )

    assert str(world.alices_session) in str(alices.structured_content)
    assert str(world.bobs_session) not in str(alices.structured_content)
    assert _BOB_NOTE not in str(alices.structured_content)
    assert str(world.bobs_session) in str(bobs.structured_content)
    assert str(world.alices_session) not in str(bobs.structured_content)


# --- AC3: no user in deps stops the run before the call ----------------------------------


async def test_run_without_user_stops_before_the_call(world: _World) -> None:
    requests: list[int] = []

    def call_once(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append(len(messages))
        return ModelResponse(parts=[ToolCallPart("query_sessions", {})])

    agent = _agent(world, FunctionModel(call_once))

    with pytest.raises(MissingUserError):
        await agent.run("What did I train?", deps=_Deps(None))

    # One model request, no retry prompt back to the model, and the tool never read.
    assert requests == [1]
    assert world.history.reads == []


# --- AC4: schema-invalid arguments leave the run recoverable --------------------------------


async def test_schema_invalid_arguments_leave_the_run_recoverable(world: _World) -> None:
    def correct_itself(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("query_sessions", {"limit": 500})])
        if any(isinstance(part, RetryPromptPart) for part in last.parts):
            return ModelResponse(parts=[ToolCallPart("query_sessions", {"limit": 5})])
        return ModelResponse(parts=[TextPart("done")])

    agent = _agent(world, FunctionModel(correct_itself))

    result = await agent.run("What did I train?", deps=_Deps(world.alice))

    assert result.output == "done"
    assert world.history.reads == [world.alice]
    (returned,) = _tool_returns(result.all_messages())
    assert str(world.alices_session) in str(returned.content)


# --- AC5: no span carries the user ID or athlete text ---------------------------------------


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """A provider wired as `main.py` wires it, behind `ScrubbingSpanExporter`. It is also made
    the global one for this test, so FastMCP's own spans, which `main.py` doesn't record
    today because it never sets a global provider, are checked too."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(exporter)))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
    yield exporter
    provider.shutdown()


def _span_text(span: ReadableSpan) -> str:
    events = [(event.name, dict(event.attributes or {})) for event in span.events]
    return repr((span.name, dict(span.attributes or {}), events, span.status.description))


@pytest.mark.parametrize("deps_user", ["alice", None], ids=["with-user", "without-user"])
async def test_tool_call_spans_carry_no_user_id_or_athlete_text(
    world: _World, spans: InMemorySpanExporter, deps_user: str | None
) -> None:
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    agent = _agent(world, TestModel(call_tools=["query_sessions"]))
    # As `main.py`'s `Agent.instrument_all`, without changing every other test's agents.
    agent.instrument = InstrumentationSettings(tracer_provider=provider, include_content=False)
    user = world.alice if deps_user else None

    try:
        await agent.run(f"How was my session? {_ALICE_NOTE}", deps=_Deps(user))
    except MissingUserError:
        assert user is None

    recorded = spans.get_finished_spans()
    scopes = {
        span.instrumentation_scope.name
        for span in recorded
        if span.instrumentation_scope is not None and "query_sessions" in span.name
    }
    # Pydantic AI's tool span always; FastMCP's and the MCP SDK's once the call is sent.
    assert scopes == ({"pydantic-ai", "fastmcp", "mcp-python-sdk"} if user else {"pydantic-ai"})
    forbidden = [str(world.alice), world.alice.hex, str(world.alices_session), _ALICE_NOTE]
    for span in recorded:
        text = _span_text(span)
        for value in forbidden:
            assert value not in text, (span.name, value)
