"""`LlmConversation`: the router and the chitchat specialist behind `ConversationModelPort`,
and the "not yet" reply (ADR-0008 skeleton routing, ticket #82)."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, PostgresDsn, SecretStr

from ai_trainer.application.ports.conversation import ReplyContext, TemplateRef
from ai_trainer.application.ports.llm_gateway import (
    LlmGatewayResult,
    LlmStreamEvent,
    StreamEnd,
    TextChunk,
)
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.llm_calls import LlmCallOutcome, ProviderPreferences
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.llm.chitchat import build_chitchat_deps
from ai_trainer.llm.conversation import (
    DRAFT_FAILED_REPLY_ID,
    DRAFT_READY_REPLY_ID,
    NOT_YET_REPLY_ID,
    SAME_SESSION_QUESTION_REPLY_ID,
    SPORT_QUESTION_REPLY_ID,
    LlmConversation,
    build_prompt_registry,
)
from ai_trainer.llm.extract import build_extract_deps
from ai_trainer.llm.router import ChatTurn, build_router_deps, router_output_type
from ai_trainer.main import build_llm_conversation
from ai_trainer.settings import Settings
from tests.chat_support import UNLOGGABLE_SPORT, sports_with_rowing

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
# 23:30 UTC on 30 Sep is already 01:30 on 1 Oct in Warsaw (ADR-0014).
_NOW = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
_THREAD = uuid4()
_GOAL = Goal(text="Send my first 7a", target_date=date(2027, 6, 1), sport_id="climbing")


@dataclass
class RecordingGateway:
    """Implements `LlmGatewayPort`; answers `run` with `run_output` and streams `chunks`."""

    run_output: dict[str, Any] | None = None
    run_outcome: LlmCallOutcome = LlmCallOutcome.SUCCESS
    chunks: list[str] = field(default_factory=lambda: ["Hey", "!"])
    runs: list[dict[str, Any]] = field(default_factory=list)
    streams: list[dict[str, Any]] = field(default_factory=list)

    async def run[OutputT: BaseModel](self, **call: Any) -> LlmGatewayResult[OutputT]:
        self.runs.append(call)
        output_type: type[OutputT] = call["output_type"]
        output = (
            output_type.model_validate(self.run_output)
            if self.run_outcome is LlmCallOutcome.SUCCESS and self.run_output is not None
            else None
        )
        error = None if output is not None else "failed"
        return LlmGatewayResult(output=output, friendly_error=error, outcome=self.run_outcome)

    async def stream(self, **call: Any) -> AsyncGenerator[LlmStreamEvent]:
        self.streams.append(call)
        for chunk in self.chunks:
            yield TextChunk(chunk)
        yield StreamEnd(
            LlmGatewayResult(
                output="".join(self.chunks), friendly_error=None, outcome=LlmCallOutcome.SUCCESS
            )
        )


_ROUTER_PREFERENCES = ProviderPreferences(sort="latency")
_EXTRACT_PREFERENCES = ProviderPreferences(preferred_max_latency={"p90": 2})


def _context(conversation_id: UUID = _THREAD, user_id: UUID | None = None) -> ReplyContext:
    history = [
        ChatMessage(
            id=uuid4(), conversation_id=_THREAD, role=ChatRole.USER, text="earlier", created_at=_NOW
        ),
        ChatMessage(
            id=uuid4(),
            conversation_id=_THREAD,
            role=ChatRole.ASSISTANT,
            text="noted",
            created_at=_NOW,
        ),
    ]
    return ReplyContext(
        user_id=user_id or uuid4(),
        conversation_id=conversation_id,
        message="log my ride, also hi",
        history=history,
        athlete_sports=["climbing", "cycling"],
        goals=[_GOAL],
        now=_NOW,
        timezone="Europe/Warsaw",
    )


def _conversation(
    gateway: RecordingGateway, sports: SportRegistry | None = None
) -> LlmConversation:
    return LlmConversation(
        registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
        gateway=gateway,
        sports=sports or default_sport_registry(),
        router_model="test/router-model",
        router_provider=_ROUTER_PREFERENCES,
        router_timeout_seconds=7.5,
        chitchat_model="test/chitchat-model",
        extract_model="test/extract-model",
        extract_provider=_EXTRACT_PREFERENCES,
        extract_timeout_seconds=9.0,
        router_reasoning_effort="low",
        extract_reasoning_effort="minimal",
        history_turns=10,
    )


_TURNS = [ChatTurn(role="user", text="earlier"), ChatTurn(role="assistant", text="noted")]


async def test_route_runs_the_router_template_and_returns_its_intents_in_order() -> None:
    gateway = RecordingGateway(
        run_output={
            "intents": [
                {"kind": "log_session", "sport": "cycling", "confidence": 0.9, "span": "ride"},
                {"kind": "chitchat", "confidence": 0.8, "span": "hi"},
            ]
        }
    )
    context = _context()

    routing = await _conversation(gateway).route(context)

    assert routing.outcome is LlmCallOutcome.SUCCESS
    assert list(routing.intents) == [
        Intent(kind="log_session", sport="cycling"),
        Intent(kind="chitchat"),
    ]
    [call] = gateway.runs
    assert (call["template_id"], call["template_version"], call["model_id"]) == (
        "router",
        3,
        "test/router-model",
    )
    assert (call["user_id"], call["prompt"]) == (context.user_id, "log my ride, also hi")
    assert call["temperature"] == 0
    assert call["output_retries"] == 2
    assert call["provider"] == _ROUTER_PREFERENCES
    assert call["timeout_seconds"] == 7.5
    assert call["reasoning_effort"] == "low"
    assert call["output_type"].model_json_schema() == (
        router_output_type(default_sport_registry()).model_json_schema()
    )
    deps = build_router_deps(
        message=context.message,
        history=_TURNS,
        athlete_sports=["climbing", "cycling"],
        now=_NOW,
        timezone="Europe/Warsaw",
        history_turns=10,
    )
    expected = build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()).render_instructions(
        "router", 3, deps
    )
    assert call["instructions"] == expected
    # No persona (ADR-0026 step 6).
    assert expected.startswith("# Task: route the athlete's message")


async def test_a_failed_router_call_routes_nowhere_and_reports_its_outcome() -> None:
    gateway = RecordingGateway(run_outcome=LlmCallOutcome.TIMEOUT)

    routing = await _conversation(gateway).route(_context())

    assert (list(routing.intents), routing.outcome) == ([], LlmCallOutcome.TIMEOUT)


async def test_stream_chitchat_streams_the_chitchat_template_through_the_gateway() -> None:
    gateway = RecordingGateway(chunks=["Hey", "!"])
    context = _context()

    events = [event async for event in _conversation(gateway).stream_chitchat(context)]

    assert [event.text for event in events if isinstance(event, TextChunk)] == ["Hey", "!"]
    [call] = gateway.streams
    assert (call["template_id"], call["template_version"], call["model_id"]) == (
        "chitchat",
        1,
        "test/chitchat-model",
    )
    deps = build_chitchat_deps(
        message=context.message,
        history=_TURNS,
        athlete_sports=["climbing", "cycling"],
        goals=[_GOAL],
        now=_NOW,
        timezone="Europe/Warsaw",
        history_turns=10,
    )
    expected = build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()).render_instructions(
        "chitchat", 1, deps
    )
    assert call["instructions"] == expected
    assert "01:30" in call["instructions"]
    assert call["prompt"] == "log my ride, also hi"


def test_chitchat_template_names_the_template_that_streams() -> None:
    conversation = _conversation(RecordingGateway())

    assert conversation.chitchat_template == TemplateRef(id="chitchat", version=1)


def test_not_yet_is_read_from_its_file_with_no_model_call() -> None:
    gateway = RecordingGateway()

    reply = _conversation(gateway).not_yet()

    assert reply.template == TemplateRef(id=NOT_YET_REPLY_ID, version=1)
    assert reply.text == (_PROMPTS_ROOT / "not_yet" / "v1.en.md").read_text().strip()
    assert reply.text
    assert (gateway.runs, gateway.streams) == ([], [])


_CLIMB_OUTPUT = {
    "occurred_on": "2026-10-01",
    "date_source": "assumed_today",
    "activities": [
        {"kind": "boulder", "count": 3, "grade": "6A"},
        {"kind": "boulder", "count": 2, "grade": "6B"},
    ],
}


async def test_extract_runs_the_sports_extraction_template_through_the_gateway() -> None:
    gateway = RecordingGateway(run_output=_CLIMB_OUTPUT)
    context = _context()

    extraction = await _conversation(gateway).extract(context, "climbing")

    assert extraction.outcome is LlmCallOutcome.SUCCESS
    assert extraction.output == ClimbingExtraction.model_validate(_CLIMB_OUTPUT)
    assert extraction.template == TemplateRef(id="climbing.extract", version=2)
    [call] = gateway.runs
    assert (call["template_id"], call["template_version"], call["model_id"]) == (
        "climbing.extract",
        2,
        "test/extract-model",
    )
    assert call["output_type"] is ClimbingExtraction
    assert (call["user_id"], call["prompt"]) == (context.user_id, context.message)
    assert (call["temperature"], call["output_retries"]) == (0, 2)
    assert call["provider"] == _EXTRACT_PREFERENCES
    assert call["timeout_seconds"] == 9.0
    assert call["reasoning_effort"] == "minimal"
    deps = build_extract_deps(
        message=context.message,
        history=_TURNS,
        now=_NOW,
        timezone="Europe/Warsaw",
        history_turns=10,
    )
    expected = build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()).render_instructions(
        "climbing.extract", 2, deps
    )
    assert call["instructions"] == expected
    # The athlete's own turn only, and no persona (ADR-0026 step 6).
    assert "- earlier\n" in call["instructions"]
    assert "noted" not in call["instructions"]
    assert "training companion" not in call["instructions"]


async def test_a_failed_extraction_returns_no_draft_and_its_outcome() -> None:
    gateway = RecordingGateway(run_outcome=LlmCallOutcome.TIMEOUT)

    extraction = await _conversation(gateway).extract(_context(), "climbing")

    assert (extraction.output, extraction.outcome) == (None, LlmCallOutcome.TIMEOUT)


def test_draft_ready_is_read_from_its_file_with_no_model_call() -> None:
    gateway = RecordingGateway()

    reply = _conversation(gateway).draft_ready()

    assert reply.template == TemplateRef(id=DRAFT_READY_REPLY_ID, version=1)
    assert reply.text == (_PROMPTS_ROOT / "draft_ready" / "v1.en.md").read_text().strip()
    assert reply.text
    assert (gateway.runs, gateway.streams) == ([], [])


def test_draft_failed_is_read_from_its_file_with_no_model_call() -> None:
    gateway = RecordingGateway()

    reply = _conversation(gateway).draft_failed()

    assert reply.template == TemplateRef(id=DRAFT_FAILED_REPLY_ID, version=1)
    assert reply.text == (_PROMPTS_ROOT / "draft_failed" / "v1.en.md").read_text().strip()
    assert reply.text
    assert (gateway.runs, gateway.streams) == ([], [])


def test_sport_question_is_read_from_its_file_with_no_model_call() -> None:
    gateway = RecordingGateway()

    reply = _conversation(gateway).sport_question()

    assert reply.template == TemplateRef(id=SPORT_QUESTION_REPLY_ID, version=1)
    assert reply.text == (_PROMPTS_ROOT / "sport_question" / "v1.en.md").read_text().strip()
    assert reply.text
    assert (gateway.runs, gateway.streams) == ([], [])


def test_same_session_question_is_read_from_its_file_with_no_model_call() -> None:
    gateway = RecordingGateway()

    reply = _conversation(gateway).same_session_question()

    assert reply.template == TemplateRef(id=SAME_SESSION_QUESTION_REPLY_ID, version=1)
    assert reply.text == (_PROMPTS_ROOT / "same_session_question" / "v1.en.md").read_text().strip()
    assert reply.text
    assert (gateway.runs, gateway.streams) == ([], [])


async def test_extract_refuses_a_sport_that_names_no_extraction_template() -> None:
    gateway = RecordingGateway()

    with pytest.raises(ValueError, match=UNLOGGABLE_SPORT):
        await _conversation(gateway, sports_with_rowing()).extract(_context(), UNLOGGABLE_SPORT)

    assert gateway.runs == []


async def test_the_app_sends_each_templates_reasoning_effort_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        router_reasoning_effort="low",
        extract_reasoning_effort="high",
    )
    gateway = RecordingGateway(run_output=_CLIMB_OUTPUT)
    conversation = build_llm_conversation(settings, default_sport_registry(), gateway)

    await conversation.extract(_context(), "climbing")
    gateway.run_output = {"intents": [{"kind": "chitchat", "confidence": 0.8, "span": "hi"}]}
    await conversation.route(_context())

    assert [call["reasoning_effort"] for call in gateway.runs] == ["high", "low"]


async def test_the_app_sends_each_templates_provider_preferences_and_timeout_from_settings() -> (
    None
):
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        router_provider_preferences=_ROUTER_PREFERENCES,
        extract_provider_preferences=_EXTRACT_PREFERENCES,
        router_call_timeout_seconds=4.0,
        extract_call_timeout_seconds=6.0,
    )
    gateway = RecordingGateway(run_output=_CLIMB_OUTPUT)
    conversation = build_llm_conversation(settings, default_sport_registry(), gateway)

    await conversation.extract(_context(), "climbing")
    gateway.run_output = {"intents": [{"kind": "chitchat", "confidence": 0.8, "span": "hi"}]}
    await conversation.route(_context())

    assert [(call["provider"], call["timeout_seconds"]) for call in gateway.runs] == [
        (_EXTRACT_PREFERENCES, 6.0),
        (_ROUTER_PREFERENCES, 4.0),
    ]


async def test_unset_template_timeouts_leave_the_gateway_default() -> None:
    settings = Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
    )
    gateway = RecordingGateway(run_output=_CLIMB_OUTPUT)
    conversation = build_llm_conversation(settings, default_sport_registry(), gateway)

    await conversation.extract(_context(), "climbing")

    [call] = gateway.runs
    assert (call["provider"], call["timeout_seconds"]) == (None, None)


async def _session_ids(gateway: RecordingGateway, *contexts: ReplyContext) -> list[str]:
    conversation = _conversation(gateway)
    for context in contexts:
        gateway.run_output = {"intents": [{"kind": "chitchat", "confidence": 0.8, "span": "hi"}]}
        await conversation.route(context)
        gateway.run_output = _CLIMB_OUTPUT
        await conversation.extract(context, "climbing")
    return [call["session_id"] for call in gateway.runs]


async def test_calls_in_one_conversation_share_a_session_id_and_another_gets_its_own() -> None:
    """Follow-ups stay on the upstream that holds their warm cache (ADR-0026 step 4)."""
    gateway = RecordingGateway()
    other_thread = uuid4()

    ids = await _session_ids(gateway, _context(), _context(), _context(other_thread))

    assert ids[0] == ids[1] == ids[2] == ids[3]
    assert ids[4] == ids[5]
    assert ids[4] != ids[0]


async def test_the_session_id_carries_no_user_identity() -> None:
    """Invariant 4 of ADR-0026: opaque, never a user ID or email (ADR-0018, ADR-0020)."""
    user_id = uuid4()
    gateway = RecordingGateway()

    [routed, extracted] = await _session_ids(gateway, _context(user_id=user_id))

    assert routed == extracted
    assert 0 < len(routed) <= 256  # OpenRouter's limit
    for identity in (str(user_id), user_id.hex, "athlete@example.com"):
        assert identity not in routed
    # Not the conversation's own database ID either, so nothing sent links back to a row.
    assert str(_THREAD) not in routed
    assert _THREAD.hex not in routed
