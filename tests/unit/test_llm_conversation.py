"""`LlmConversation`: the router and the chitchat specialist behind `ConversationModelPort`,
and the "not yet" reply (ADR-0008 skeleton routing, ticket #82)."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

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
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.chitchat import build_chitchat_deps
from ai_trainer.llm.conversation import NOT_YET_REPLY_ID, LlmConversation, build_prompt_registry
from ai_trainer.llm.router import ChatTurn, build_router_deps, router_output_type

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
# 23:30 UTC on 30 Sep is already 01:30 on 1 Oct in Warsaw (ADR-0014).
_NOW = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
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


def _context() -> ReplyContext:
    history = [
        ChatMessage(id=uuid4(), role=ChatRole.USER, text="earlier", created_at=_NOW),
        ChatMessage(id=uuid4(), role=ChatRole.ASSISTANT, text="noted", created_at=_NOW),
    ]
    return ReplyContext(
        user_id=uuid4(),
        message="log my ride, also hi",
        history=history,
        athlete_sports=["climbing", "cycling"],
        goals=[_GOAL],
        now=_NOW,
        timezone="Europe/Warsaw",
    )


def _conversation(gateway: RecordingGateway) -> LlmConversation:
    return LlmConversation(
        registry=build_prompt_registry(_PROMPTS_ROOT, default_sport_registry()),
        gateway=gateway,
        sports=default_sport_registry(),
        router_model="test/router-model",
        chitchat_model="test/chitchat-model",
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
        1,
        "test/router-model",
    )
    assert (call["user_id"], call["prompt"]) == (context.user_id, "log my ride, also hi")
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
        "router", 1, deps
    )
    assert call["instructions"] == expected


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
