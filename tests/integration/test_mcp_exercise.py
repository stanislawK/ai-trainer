"""`find_exercise` and `exercise_history` in process on PostgreSQL (PRD-0010 B37, ADR-0010,
ADR-0031, ticket #401): through `Client(server)` and the real `process_tool_call` hook, on the
real repositories and the sample catalogue. Both tools read only the caller's own data."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_history import SqlAlchemyExerciseHistory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.orm import ExerciseOrm, ExerciseTextOrm, GymExerciseEntryOrm, GymSetOrm
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.application.exercise_catalogue import import_catalogue
from ai_trainer.application.exercise_lookup import ExerciseFinder
from ai_trainer.application.exercise_resolution import ResolutionLimits
from ai_trainer.application.ports.exercises import ExerciseHistoryPort
from ai_trainer.application.training_history import ExerciseTrainingHistory, TrainingHistory
from ai_trainer.domain.exercises import ExerciseEntry, ExerciseMeasure
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.mcp.identity import USER_ID_META_KEY, MissingUserError
from ai_trainer.mcp.server import build_mcp_server
from ai_trainer.mcp.toolset import build_toolset
from tests.activity_support import STORES
from tests.exercise_support import sample_source
from tests.integration.test_mcp_query_sessions import _session, _user

SessionFactory = Callable[[], AsyncSession]
_LIMITS = ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4)
_BOBS_EXERCISE = "bobs secret cable thing"


@dataclass(frozen=True)
class _Deps:
    user_id: UUID | None


class _BrokenHistory:
    """A history port that fails the way a lost database connection would."""

    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        raise RuntimeError(f"connection lost for {user_id}")


@dataclass(frozen=True)
class _World:
    alice: UUID
    bob: UUID
    bench: UUID
    alices_custom: UUID
    bobs_custom: UUID
    server_with: Callable[[ExerciseHistoryPort | None], Any]


async def _entry(
    factory: SessionFactory,
    user_id: UUID,
    exercise_id: UUID,
    day: date,
    sets: Sequence[tuple[int, Decimal, str]],
) -> None:
    session_id = await _session(factory, user_id, day, "", "gym")
    entry_id = uuid4()
    async with factory() as db:
        db.add(
            GymExerciseEntryOrm(
                id=entry_id,
                user_id=user_id,
                session_id=session_id,
                position=1,
                exercise_id=exercise_id,
                name_as_typed="bench press",
                measure=ExerciseMeasure.LOAD_REPS.value,
            )
        )
        await db.flush()
        db.add_all(
            GymSetOrm(
                user_id=user_id,
                entry_id=entry_id,
                set_no=number,
                reps=reps,
                load_kg=load,
                load_original=original,
            )
            for number, (reps, load, original) in enumerate(sets, start=1)
        )
        await db.commit()


@pytest.fixture
async def world(db_session_factory: SessionFactory) -> _World:
    exercises = SqlAlchemyExerciseRepository(db_session_factory)
    await import_catalogue(source=sample_source(), exercises=exercises)
    alice = await _user(db_session_factory, f"mcp-ex-alice-{uuid4()}")
    bob = await _user(db_session_factory, f"mcp-ex-bob-{uuid4()}")
    async with db_session_factory() as db:
        bench = await db.scalar(
            select(ExerciseOrm.id).where(ExerciseOrm.source_ref == "zz-test-01")
        )
    assert bench is not None
    alices_custom = await exercises.create_custom(
        alice, sport_id="gym", name="alice cable fly", default_measure=ExerciseMeasure.LOAD_REPS
    )
    bobs_custom = await exercises.create_custom(
        bob, sport_id="gym", name=_BOBS_EXERCISE, default_measure=ExerciseMeasure.LOAD_REPS
    )
    await exercises.add_user_alias(alice, bench, "bench press")
    await _entry(db_session_factory, alice, bench, date(2026, 10, 12), [(8, Decimal("60"), "60kg")])
    await _entry(
        db_session_factory,
        alice,
        bench,
        date(2026, 10, 19),
        [(8, Decimal("62.5"), "62,5kg"), (6, Decimal("65"), "65kg")],
    )
    await _entry(db_session_factory, bob, bench, date(2026, 10, 20), [(5, Decimal("100"), "100kg")])
    await _entry(
        db_session_factory, bob, bobs_custom.id, date(2026, 10, 21), [(10, Decimal("20"), "20kg")]
    )

    def server_with(history: ExerciseHistoryPort | None) -> Any:
        return build_mcp_server(
            TrainingHistory(
                SqlAlchemyTrainingSessionRepository(db_session_factory, stores=STORES),
                default_sport_registry(),
            ),
            ExerciseFinder(exercises, _LIMITS),
            ExerciseTrainingHistory(
                exercises, history or SqlAlchemyExerciseHistory(db_session_factory)
            ),
        )

    return _World(alice, bob, bench, alices_custom.id, bobs_custom.id, server_with)


def _meta(user_id: UUID) -> dict[str, Any]:
    return {USER_ID_META_KEY: str(user_id)}


async def _call(world: _World, user_id: UUID, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with Client(world.server_with(None)) as client:
        result = await client.call_tool(tool, args, meta=_meta(user_id))
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


# --- AC1: the settled exercise first ---------------------------------------------------------


async def test_mcp_exercise_find_returns_settled_exercise_first(world: _World) -> None:
    found = await _call(world, world.alice, "find_exercise", {"name": "bench press"})

    assert found["success"] is True
    assert found["data"]["source"] == "history"
    (only,) = found["data"]["exercises"]
    assert only["id"] == str(world.bench)
    assert only["name"] == "Barbell Bench Press"
    assert only["equipment"] == "barbell"


async def test_mcp_exercise_find_shortlists_up_to_four_with_descriptions(world: _World) -> None:
    found = await _call(world, world.bob, "find_exercise", {"name": "chest press"})

    assert found["data"]["source"] == "catalogue"
    exercises = found["data"]["exercises"]
    assert 1 <= len(exercises) <= 4
    assert "Lever Chest Press" in {exercise["name"] for exercise in exercises}
    assert all(exercise["description"] for exercise in exercises)


async def test_mcp_exercise_find_describes_an_own_exercise_only_once_it_has_a_text(
    world: _World, db_session_factory: SessionFactory
) -> None:
    before = await _call(world, world.alice, "find_exercise", {"name": "alice cable fly"})
    async with db_session_factory() as db:
        # Only a Polish text exists, so it is the fallback; it is longer than a short description.
        db.add(
            ExerciseTextOrm(
                exercise_id=world.alices_custom, lang="pl", instructions="x" * 500, steps=[]
            )
        )
        await db.commit()
    after = await _call(world, world.alice, "find_exercise", {"name": "alice cable fly"})

    assert before["data"]["source"] == "catalogue"
    assert before["data"]["exercises"][0]["description"] is None
    description = after["data"]["exercises"][0]["description"]
    assert len(description) == 200
    assert description.endswith("…")


async def test_mcp_exercise_find_with_no_match_is_a_failed_result_with_a_hint(
    world: _World,
) -> None:
    found = await _call(world, world.alice, "find_exercise", {"name": "qqqzzzxxx"})

    assert found["success"] is False
    assert found["recovery_hint"]


async def test_mcp_exercise_find_with_a_blank_name_is_a_failed_result_with_a_hint(
    world: _World,
) -> None:
    found = await _call(world, world.alice, "find_exercise", {"name": "   "})

    assert found["success"] is False
    assert found["recovery_hint"]


# --- AC2: sets, loads and dates --------------------------------------------------------------


async def test_mcp_exercise_history_returns_sets_loads_and_dates_newest_first(
    world: _World,
) -> None:
    history = await _call(world, world.alice, "exercise_history", {"exercise_id": str(world.bench)})

    assert history["success"] is True
    page = history["data"]
    assert page["exercise"]["name"] == "Barbell Bench Press"
    assert page["truncated"] is False
    assert [entry["occurred_on"] for entry in page["entries"]] == ["2026-10-19", "2026-10-12"]
    latest = page["entries"][0]
    assert latest["name_as_typed"] == "bench press"
    assert [(s["set_no"], s["reps"], s["load_kg"], s["load_original"]) for s in latest["sets"]] == [
        (1, 8, 62.5, "62,5kg"),
        (2, 6, 65.0, "65kg"),
    ]


async def test_mcp_exercise_history_says_when_it_cut_the_page(world: _World) -> None:
    history = await _call(
        world, world.alice, "exercise_history", {"exercise_id": str(world.bench), "limit": 1}
    )

    assert history["data"]["truncated"] is True
    assert [entry["occurred_on"] for entry in history["data"]["entries"]] == ["2026-10-19"]


async def test_mcp_exercise_history_of_an_unlogged_exercise_is_a_failed_result(
    world: _World,
) -> None:
    history = await _call(
        world, world.alice, "exercise_history", {"exercise_id": str(world.alices_custom)}
    )

    assert history["success"] is False
    assert history["recovery_hint"]


# --- AC3: tenancy ----------------------------------------------------------------------------


async def test_mcp_exercise_other_athletes_custom_exercise_and_entries_never_appear(
    world: _World,
) -> None:
    found = await _call(world, world.alice, "find_exercise", {"name": _BOBS_EXERCISE})
    foreign = await _call(
        world, world.alice, "exercise_history", {"exercise_id": str(world.bobs_custom)}
    )
    shared = await _call(world, world.alice, "exercise_history", {"exercise_id": str(world.bench)})
    bobs = await _call(world, world.bob, "exercise_history", {"exercise_id": str(world.bench)})

    assert _BOBS_EXERCISE not in str(found)
    assert str(world.bobs_custom) not in str(found)
    assert foreign["success"] is False
    assert _BOBS_EXERCISE not in str(foreign)
    # A catalogue exercise is shared; the entries under it are not.
    assert "2026-10-20" not in str(shared)
    assert "100kg" not in str(shared)
    assert [entry["occurred_on"] for entry in bobs["data"]["entries"]] == ["2026-10-20"]


# --- AC4: an unknown ID is a failed result, an unexpected failure is masked -------------------


async def test_mcp_exercise_unknown_id_is_a_failed_result_with_a_hint(world: _World) -> None:
    history = await _call(world, world.alice, "exercise_history", {"exercise_id": str(uuid4())})

    assert history["success"] is False
    assert "find_exercise" in history["recovery_hint"]


async def test_mcp_exercise_unexpected_failure_is_masked(world: _World) -> None:
    server = world.server_with(_BrokenHistory())

    async with Client(server) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool(
                "exercise_history", {"exercise_id": str(world.bench)}, meta=_meta(world.alice)
            )

    assert str(world.alice) not in str(raised.value)
    assert "connection lost" not in str(raised.value)


# --- AC5: through Client(server) and the real identity hook ----------------------------------


def _agent(world: _World, model: FunctionModel) -> Agent[_Deps, str]:
    return Agent(model, deps_type=_Deps, toolsets=[build_toolset(world.server_with(None))])


def _returns(messages: Sequence[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in getattr(message, "parts", ())
        if isinstance(part, ToolReturnPart)
    ]


async def test_mcp_exercise_agent_run_reaches_both_tools_with_the_user_from_deps(
    world: _World,
) -> None:
    def ask(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("find_exercise", {"name": "bench press"})])
        if len(messages) == 3:
            return ModelResponse(
                parts=[ToolCallPart("exercise_history", {"exercise_id": str(world.bench)})]
            )
        return ModelResponse(parts=[TextPart("done")])

    agent = _agent(world, FunctionModel(ask))

    result = await agent.run("How is my bench going?", deps=_Deps(world.alice))

    found, history = _returns(result.all_messages())[:2]
    assert str(world.bench) in found
    assert "2026-10-19" in history
    assert "2026-10-20" not in history


async def test_mcp_exercise_run_without_a_user_stops_before_the_call(world: _World) -> None:
    requests: list[int] = []

    def call_once(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        requests.append(len(messages))
        return ModelResponse(parts=[ToolCallPart("find_exercise", {"name": "bench press"})])

    agent = _agent(world, FunctionModel(call_once))

    with pytest.raises(MissingUserError):
        await agent.run("How is my bench going?", deps=_Deps(None))

    assert requests == [1]


async def test_mcp_exercise_missing_identity_raises_tool_error(world: _World) -> None:
    async with Client(world.server_with(None)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("find_exercise", {"name": "bench press"})


async def test_mcp_exercise_schema_invalid_arguments_leave_the_run_recoverable(
    world: _World,
) -> None:
    def correct_itself(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart("exercise_history", {"exercise_id": "not-an-id"})]
            )
        if len(messages) == 3:
            return ModelResponse(
                parts=[ToolCallPart("exercise_history", {"exercise_id": str(world.bench)})]
            )
        return ModelResponse(parts=[TextPart("done")])

    agent = _agent(world, FunctionModel(correct_itself))

    result = await agent.run("How is my bench going?", deps=_Deps(world.alice))

    assert "2026-10-19" in _returns(result.all_messages())[-1]
