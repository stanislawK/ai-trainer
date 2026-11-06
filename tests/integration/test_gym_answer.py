"""A gym question from routing to the saved reply (PRD 0011 B40, ADR-0010, ticket #421): the real
conversation, gateway, MCP server and `process_tool_call` hook, on the real repositories, with a
scripted model. What the tools hand the model is what the athlete's reply can be built from."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_history import SqlAlchemyExerciseHistory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.orm import ExerciseOrm
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.application.exercise_catalogue import import_catalogue
from ai_trainer.application.exercise_lookup import ExerciseFinder
from ai_trainer.application.exercise_resolution import ResolutionLimits
from ai_trainer.application.knowledge_search import KnowledgeSearch
from ai_trainer.application.ports.exercises import ExerciseHistoryPort
from ai_trainer.application.reply import ReplyDone, ReplyEvent, ReplyFailed, start_reply
from ai_trainer.application.training_history import ExerciseTrainingHistory, TrainingHistory
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.exercises import ExerciseEntry, ExerciseMeasure, LoggedName
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import PageLocator
from ai_trainer.domain.knowledge_search import CitedSource, KnowledgeHit
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import LlmConversation, build_prompt_registry
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.mcp.citations import cited_sources
from ai_trainer.mcp.server import build_mcp_server
from ai_trainer.mcp.toolset import build_toolset
from tests.activity_support import STORES
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryChatRepository,
    InMemoryProfile,
    ScriptedFastRouter,
    ScriptedQuestionSport,
    reply_services,
)
from tests.exercise_support import sample_source
from tests.mcp_support import EMBEDDING_MODEL, FixedPassages, QueryEmbeddings
from tests.integration.test_mcp_exercise import _entry, _typed_only
from tests.integration.test_mcp_query_sessions import _session, _user
from tests.unit.test_llm_gateway import FakeLlmCallsRepository, _settings

SessionFactory = Callable[[], AsyncSession]
_LIMITS = ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4)
_PROMPTS = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
_BENCH = [Intent(kind="ask_training_question")]
_BOBS_TYPED = "Bobs rising arms"
_MODEL = "z-ai/glm-5.3-flash"


class _BrokenHistory(SqlAlchemyExerciseHistory):
    """The real history, losing its connection the way a database would."""

    async def entries(
        self, user_id: UUID, exercise_id: UUID, *, limit: int
    ) -> Sequence[ExerciseEntry]:
        raise RuntimeError(f"connection lost for {user_id}")

    async def logged_names(
        self, user_id: UUID, name: str, *, near: float, limit: int
    ) -> Sequence[LoggedName]:
        raise RuntimeError(f"connection lost for {user_id}")


@dataclass(frozen=True)
class _World:
    factory: SessionFactory
    alice: UUID
    bob: UUID
    bench: UUID
    bobs_custom: UUID


@pytest.fixture
async def world(db_session_factory: SessionFactory) -> _World:
    exercises = SqlAlchemyExerciseRepository(db_session_factory)
    await import_catalogue(source=sample_source(), exercises=exercises)
    alice = await _user(db_session_factory, f"answer-alice-{uuid4()}")
    bob = await _user(db_session_factory, f"answer-bob-{uuid4()}")
    async with db_session_factory() as db:
        bench = await db.scalar(
            select(ExerciseOrm.id).where(ExerciseOrm.source_ref == "zz-test-01")
        )
    assert bench is not None
    bobs_custom = await exercises.create_custom(
        bob,
        sport_id="gym",
        name="bobs secret cable thing",
        default_measure=ExerciseMeasure.LOAD_REPS,
    )
    await _entry(db_session_factory, bob, bench, date(2026, 10, 20), [(5, Decimal("100"), "100kg")])
    await _typed_only(db_session_factory, bob, _BOBS_TYPED, date(2026, 10, 21))
    return _World(db_session_factory, alice, bob, bench, bobs_custom.id)


@dataclass
class _Turn:
    events: list[ReplyEvent]
    returned: list[str]
    gateway: OpenRouterGateway
    calls: FakeLlmCallsRepository
    requests: int


def _returned(messages: Sequence[ModelMessage]) -> list[str]:
    return [
        json.dumps(part.content, default=str)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _call(name: str, args: dict[str, Any]) -> DeltaToolCalls:
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args))}


async def _ask(
    world: _World,
    user_id: UUID,
    *,
    tool_calls: Sequence[tuple[str, dict[str, Any]]] = (),
    read_logged: bool = False,
    history: ExerciseHistoryPort | None = None,
    fail: bool = False,
    say: str = "Here is what you logged.",
    keep_calling: bool = False,
    max_requests: int = 6,
    passages: Sequence[KnowledgeHit] = (),
    question: str = "How is my bench going?",
) -> _Turn:
    """One whole reply on the real stack. The scripted model makes `tool_calls` in order, then
    reads each `logged` row `find_exercise` returned, then says `say`."""
    exercises = SqlAlchemyExerciseRepository(world.factory)
    logged = history or SqlAlchemyExerciseHistory(world.factory)
    server = build_mcp_server(
        TrainingHistory(
            SqlAlchemyTrainingSessionRepository(world.factory, stores=STORES),
            default_sport_registry(),
        ),
        ExerciseFinder(exercises, _LIMITS, logged),
        ExerciseTrainingHistory(exercises, logged),
        knowledge=KnowledgeSearch(
            embeddings=QueryEmbeddings(),
            repository=FixedPassages(passages),
            sports=default_sport_registry(),
            embedding_model=EMBEDDING_MODEL,
            depth=20,
        ),
    )
    calls = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(
        _settings(), calls, toolset=build_toolset(server), citations=cited_sources
    )
    registry = build_prompt_registry(_PROMPTS, default_sport_registry())
    conversation = LlmConversation(
        registry=registry,
        gateway=gateway,
        sports=default_sport_registry(),
        router_model=_MODEL,
        chitchat_model=_MODEL,
        answer_model=_MODEL,
        answer_max_requests=max_requests,
        extract_model=_MODEL,
        history_turns=10,
    )
    seen: list[str] = []
    requests: list[int] = []

    async def script(messages: list[ModelMessage], info: AgentInfo) -> Any:
        requests.append(len(messages))
        returned = _returned(messages)
        seen[:] = returned
        if fail:
            raise RuntimeError("upstream is down")
        if keep_calling:
            yield _call("query_sessions", {})
            return
        if len(returned) < len(tool_calls):
            yield _call(*tool_calls[len(returned)])
            return
        if read_logged:
            rows = json.loads(returned[0])["data"]["logged"]
            remaining = rows[len(returned) - len(tool_calls) :]
            if remaining:
                row = remaining[0]
                by = (
                    {"exercise_id": row["exercise_id"]}
                    if row["exercise_id"]
                    else {"typed_name": row["name"]}
                )
                yield _call("exercise_history", by)
                return
        yield say

    chat = InMemoryChatRepository()
    message = await chat.add(
        user_id,
        await chat.conversation_of(user_id),
        ChatRole.USER,
        question,
        FROZEN_NOW,
    )
    services = replace(
        reply_services(
            chat,
            profile=InMemoryProfile(sports={user_id: ["gym"]}),
            fast_router=ScriptedFastRouter(intents=_BENCH),
            question_sport=ScriptedQuestionSport(sport="gym"),
        ),
        conversation=conversation,
        sports=default_sport_registry(),
    )
    with gateway.tool_agent.override(model=FunctionModel(stream_function=script)):
        stream = await start_reply(user_id, message.id, timezone="Europe/Warsaw", services=services)
        events = [event async for event in stream]
    return _Turn(events, seen, gateway, calls, len(requests))


_FIND = ("find_exercise", {"name": "bench"})


async def test_bench_question_answers_from_the_athletes_sets_and_dates(world: _World) -> None:
    await _entry(
        world.factory, world.alice, world.bench, date(2026, 10, 28), [(8, Decimal("45"), "45kg")]
    )
    await _entry(
        world.factory, world.alice, world.bench, date(2026, 11, 1), [(8, Decimal("47.5"), "47,5kg")]
    )

    turn = await _ask(world, world.alice, tool_calls=[_FIND], read_logged=True, say="45 then 47.5.")

    (found, history) = turn.returned
    assert str(world.bench) in found
    assert "2026-10-28" in history
    assert "2026-11-01" in history
    assert "47,5kg" in history
    done = turn.events[-1]
    assert isinstance(done, ReplyDone)
    (reply,) = done.messages
    assert (reply.text, reply.sports) == ("45 then 47.5.", ("gym",))
    assert (reply.template_id, reply.template_version) == ("gym.answer", 2)


async def test_the_reply_writes_one_llm_call_for_the_whole_tool_loop(world: _World) -> None:
    await _entry(
        world.factory, world.alice, world.bench, date(2026, 10, 28), [(8, Decimal("45"), "45kg")]
    )

    turn = await _ask(world, world.alice, tool_calls=[_FIND], read_logged=True)

    assert turn.requests == 3
    (row,) = turn.calls.recorded
    assert (row.template_id, row.user_id, row.outcome) == (
        "gym.answer",
        world.alice,
        LlmCallOutcome.SUCCESS,
    )


async def test_typed_only_entries_count_as_history(world: _World) -> None:
    """Backfilled sessions and names never settled have no exercise ID (ADR-0031)."""
    await _typed_only(world.factory, world.alice, "Bench press", date(2026, 10, 28))

    turn = await _ask(world, world.alice, tool_calls=[_FIND], read_logged=True)

    (found, history) = turn.returned
    assert '"exercise_id":null' in found.replace(" ", "")
    assert '"success":true' in history.replace(" ", "")
    assert "2026-10-28" in history


async def test_no_history_reaches_the_model_as_no_entries(world: _World) -> None:
    turn = await _ask(world, world.alice, tool_calls=[_FIND], read_logged=True)

    (found,) = turn.returned
    assert '"logged":[]' in found.replace(" ", "")
    # Bob benches, and the catalogue is shared, but none of his sets reach Alice's reply.
    assert "100kg" not in found


async def test_one_athlete_never_reads_anothers_sessions(world: _World) -> None:
    """Every way a model could ask, through the real hook: Bob's ID, Bob's typed name, all days."""
    await _session(world.factory, world.alice, date(2026, 10, 19), "alice's own day", "gym")

    turn = await _ask(
        world,
        world.alice,
        tool_calls=[
            ("exercise_history", {"exercise_id": str(world.bobs_custom)}),
            ("exercise_history", {"exercise_id": str(world.bench)}),
            ("exercise_history", {"typed_name": _BOBS_TYPED}),
            ("find_exercise", {"name": _BOBS_TYPED}),
            ("query_sessions", {"limit": 50}),
        ],
    )

    custom, shared, typed, found, sessions = turn.returned
    assert '"success":false' in custom.replace(" ", "")
    assert '"success":false' in shared.replace(" ", "")
    assert '"success":false' in typed.replace(" ", "")
    assert _BOBS_TYPED not in found
    assert "alice's own day" in sessions
    for text in turn.returned:
        assert "100kg" not in text
        assert "2026-10-20" not in text
        assert "2026-10-21" not in text
        assert str(world.bob) not in text


async def test_a_failing_tool_ends_in_the_retry_row(world: _World) -> None:
    turn = await _ask(
        world,
        world.alice,
        tool_calls=[_FIND, _FIND, _FIND],
        history=_BrokenHistory(world.factory),
    )

    # The model was told of each failure and got to retry, then the run gave up.
    assert turn.requests >= 2
    assert turn.events[-1] == ReplyFailed(outcome=LlmCallOutcome.ERROR)
    assert [row.outcome for row in turn.calls.recorded] == [LlmCallOutcome.ERROR]


async def test_a_model_failure_ends_in_the_retry_row(world: _World) -> None:
    turn = await _ask(world, world.alice, fail=True)

    assert turn.requests == 1
    assert turn.events[-1] == ReplyFailed(outcome=LlmCallOutcome.ERROR)
    assert [row.outcome for row in turn.calls.recorded] == [LlmCallOutcome.ERROR]


async def test_the_request_cap_ends_in_the_retry_row(world: _World) -> None:
    turn = await _ask(world, world.alice, keep_calling=True, max_requests=3)

    assert turn.requests == 3
    assert turn.events[-1] == ReplyFailed(outcome=LlmCallOutcome.ERROR)
    assert [row.outcome for row in turn.calls.recorded] == [LlmCallOutcome.ERROR]


# Cited answers (PRD B8, ADR-0036 #428): the same stack, with `search_knowledge` over fixed
# passages and the app's citation reader on the gateway.

_DELOAD = KnowledgeHit(
    chunk_id=uuid4(),
    text="Plan a deload week every fourth to sixth week.",
    heading_path=("Deload",),
    language=SourceLanguage.EN,
    source_id=uuid4(),
    title="Periodization Training for Sports",
    author="Tudor Bompa",
    evidence_tier=EvidenceTier.COACHING_BOOK,
    source_type=SourceType.BOOK,
    source_url=None,
    attribution="Bompa, CC BY 4.0",
    locator=PageLocator(page=212),
    exercise_id=None,
)
_UNRELATED = _DELOAD.model_copy(
    update={"chunk_id": uuid4(), "source_id": uuid4(), "title": "Bench Press Technique"}
)
_SEARCH = ("search_knowledge", {"query": "How often should I deload?", "sport": "gym"})


async def test_a_deload_question_answers_from_a_matching_source_and_saves_it(
    world: _World,
) -> None:
    turn = await _ask(
        world,
        world.alice,
        question="How often should I deload?",
        passages=[_DELOAD, _UNRELATED],
        tool_calls=[_SEARCH, ("cite_sources", {"chunk_ids": [str(_DELOAD.chunk_id)]})],
        say="Bompa suggests a deload week every 4–6 weeks.",
    )

    done = turn.events[-1]
    assert isinstance(done, ReplyDone)
    (reply,) = done.messages
    assert reply.text == "Bompa suggests a deload week every 4–6 weeks."
    assert reply.sources == (CitedSource.of(_DELOAD),)
    assert [row.outcome for row in turn.calls.recorded] == [LlmCallOutcome.SUCCESS]


async def test_a_reply_that_cites_no_passage_saves_no_sources(world: _World) -> None:
    turn = await _ask(
        world,
        world.alice,
        question="How often should I deload?",
        passages=[_UNRELATED],
        tool_calls=[_SEARCH],
        say="I have no source for that yet.",
    )

    done = turn.events[-1]
    assert isinstance(done, ReplyDone)
    assert done.messages[0].sources == ()


async def test_a_cited_id_no_search_returned_is_not_shown(world: _World) -> None:
    turn = await _ask(
        world,
        world.alice,
        question="How often should I deload?",
        passages=[_DELOAD],
        tool_calls=[_SEARCH, ("cite_sources", {"chunk_ids": [str(uuid4())]})],
        say="Bompa suggests a deload week every 4–6 weeks.",
    )

    done = turn.events[-1]
    assert isinstance(done, ReplyDone)
    assert done.messages[0].sources == ()
