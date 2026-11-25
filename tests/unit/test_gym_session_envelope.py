"""A class's length and effort reach the session draft's envelope (PRD 0013 B41, ADR-0022,
ticket #622): the extraction reports them, its sport's plugin reads them, and the draft carries
them as `duration_min` and `rpe`. No core code knows the sport."""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.base import SessionEnvelope
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.extract import build_extract_deps
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryChatRepository,
    InMemoryDraftRepository,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
_TODAY = date(2026, 10, 2)
_YOGA = "yoga class 60 min, hard"
_YOGA_EXTRACTION: dict[str, Any] = {
    "occurred_on": "2026-10-02",
    "date_source": "assumed_today",
    "activities": [{"name": "yoga class", "duration": {"value": 60, "unit": "min"}}],
    "session_duration": {"value": 60, "unit": "min"},
    "effort": "hard",
}


async def test_a_gym_draft_carries_the_session_duration_and_rpe() -> None:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    conversation = ScriptedConversation(
        intents=[[Intent(kind="log_session", sport="gym")]],
        extractions=[GymExtraction.model_validate(_YOGA_EXTRACTION)],
    )
    chat: InMemoryChatRepository = include_chat(
        app, build_templates(), conversation=conversation, drafts=drafts
    )
    user = sign_in_as(app)
    asked = await chat.add(
        user.id, await chat.conversation_of(user.id), ChatRole.USER, _YOGA, FROZEN_NOW
    )

    TestClient(app).get(f"/messages/{asked.id}/reply")

    (stored,) = drafts.all_of(user.id)
    assert (stored.draft.duration_min, stored.draft.rpe) == (60, 7)


async def test_the_gym_template_reads_the_session_fields_from_the_model() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, _YOGA_EXTRACTION)])

    registry = build_prompt_registry(_PROMPTS_ROOT, default_sport_registry())
    agent = registry.build_agent("gym.extract", 7)
    deps = build_extract_deps(
        message=_YOGA,
        history=[],
        now=datetime(2026, 10, 2, 18, 0, tzinfo=UTC),
        timezone="Europe/Warsaw",
        history_turns=4,
    )

    result = await agent.run(_YOGA, deps=deps, model=FunctionModel(respond))

    assert isinstance(result.output, GymExtraction)
    assert result.output.envelope() == SessionEnvelope(duration_min=60, rpe=7)


def test_other_sports_leave_the_envelope_empty() -> None:
    climb = ClimbingExtraction.model_validate(
        {
            "occurred_on": _TODAY,
            "date_source": "assumed_today",
            "activities": [{"kind": "boulder", "count": 3, "grade": "6A"}],
        }
    )
    ride = CyclingExtraction.model_validate(
        {
            "occurred_on": _TODAY,
            "date_source": "assumed_today",
            "activities": [{"moving_time_min": 45}],
        }
    )

    assert climb.envelope() == SessionEnvelope()
    assert ride.envelope() == SessionEnvelope(duration_min=None, rpe=None)
