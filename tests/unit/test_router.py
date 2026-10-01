"""The `router` template: intents, deps and sport options (ADR-0008, ticket #80)."""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, UnexpectedModelBehavior
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.domain.sports.base import SportPlugin
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate, PromptTemplateVariableError
from ai_trainer.llm.router import (
    INTENT_KINDS,
    ROUTER_TEMPLATE_ID,
    ChatTurn,
    RouterDeps,
    build_router_deps,
    recent_turns,
    router_output_type,
    router_template,
)

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "prompts"


def _fourth_sport_registry() -> SportRegistry:
    return SportRegistry(
        [
            *default_sport_registry().all(),
            SportPlugin(id="running", label_key="sport.running.label", icon="footprints"),
        ]
    )


def _deps(**overrides: Any) -> RouterDeps:
    values: dict[str, Any] = {
        "message": "Did 5x5 squats at 100 kg",
        "history": [],
        "athlete_sports": ["gym"],
        "today": "2026-09-30",
        "now_local": "2026-09-30T18:00:00+02:00",
        "timezone": "Europe/Warsaw",
    }
    return RouterDeps.model_validate(values | overrides)


def test_every_adr_0008_intent_kind_is_declared() -> None:
    assert INTENT_KINDS == (
        "log_session",
        "edit_session",
        "ask_training_question",
        "request_plan",
        "adjust_plan",
        "request_report",
        "update_profile",
        "wellbeing_or_injury",
        "chitchat",
        "unclear",
    )


def test_output_accepts_a_mixed_log_and_question() -> None:
    output_type = router_output_type(default_sport_registry())

    output = output_type.model_validate(
        {
            "intents": [
                {"kind": "log_session", "sport": "climbing", "confidence": 0.9, "span": "did 6A"},
                {"kind": "ask_training_question", "confidence": 0.8, "span": "is that hard?"},
            ]
        }
    )

    assert [intent.kind for intent in output.intents] == ["log_session", "ask_training_question"]  # type: ignore[attr-defined]


def test_output_rejects_an_unknown_intent_kind() -> None:
    output_type = router_output_type(default_sport_registry())

    with pytest.raises(ValidationError):
        output_type.model_validate(
            {"intents": [{"kind": "book_a_flight", "confidence": 0.9, "span": "book me a flight"}]}
        )


def test_output_rejects_an_empty_intent_list() -> None:
    with pytest.raises(ValidationError):
        router_output_type(default_sport_registry()).model_validate({"intents": []})


async def test_the_agent_rejects_and_retries_an_unknown_intent_kind() -> None:
    registry = PromptRegistry(root=_PROMPTS_ROOT)
    registry.register(router_template(default_sport_registry()))
    agent = registry.build_agent(ROUTER_TEMPLATE_ID, 1)
    calls = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"intents": [{"kind": "zzz", "confidence": 0.9, "span": "x"}]},
                )
            ]
        )

    with pytest.raises(UnexpectedModelBehavior):
        await agent.run("x", deps=_deps(), model=FunctionModel(respond))

    assert calls == 2


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_output_rejects_a_confidence_outside_zero_to_one(confidence: float) -> None:
    output_type = router_output_type(default_sport_registry())

    with pytest.raises(ValidationError):
        output_type.model_validate(
            {"intents": [{"kind": "chitchat", "confidence": confidence, "span": "hi"}]}
        )


def test_output_rejects_a_log_session_without_a_sport_from_the_registry() -> None:
    output_type = router_output_type(default_sport_registry())

    with pytest.raises(ValidationError):
        output_type.model_validate(
            {"intents": [{"kind": "log_session", "sport": "running", "confidence": 1, "span": "x"}]}
        )
    with pytest.raises(ValidationError):
        output_type.model_validate(
            {"intents": [{"kind": "log_session", "confidence": 1, "span": "x"}]}
        )


def test_a_fourth_sport_reaches_the_router_options_without_a_template_edit() -> None:
    payload = {
        "intents": [{"kind": "log_session", "sport": "running", "confidence": 0.9, "span": "10k"}]
    }

    with pytest.raises(ValidationError):
        router_output_type(default_sport_registry()).model_validate(payload)
    output = router_output_type(_fourth_sport_registry()).model_validate(payload)

    assert output.intents[0].sport == "running"  # type: ignore[attr-defined]
    schema = router_template(_fourth_sport_registry()).output_type.model_json_schema()
    assert "running" in str(schema)


async def test_the_fourth_sport_is_part_of_the_schema_the_model_is_given() -> None:
    registry = PromptRegistry(root=_PROMPTS_ROOT)
    registry.register(router_template(_fourth_sport_registry()))
    agent = registry.build_agent(ROUTER_TEMPLATE_ID, 1)
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(info.output_tools))
        return ModelResponse(parts=[TextPart("{}")])

    with pytest.raises(Exception):  # noqa: B017, PT011 -- the scripted reply is not valid output
        await agent.run("x", deps=_deps(), model=FunctionModel(respond))

    assert "running" in seen[0]


def test_the_router_agent_builds_from_the_real_template_file() -> None:
    registry = PromptRegistry(root=_PROMPTS_ROOT)
    registry.register(router_template(default_sport_registry()))

    agent = registry.build_agent(ROUTER_TEMPLATE_ID, 1)

    assert agent is not None


def test_a_router_variable_missing_from_the_deps_model_fails_the_build() -> None:
    class _NarrowDeps(BaseModel):
        message: str

    narrowed = PromptTemplate(
        id="broken",
        version=1,
        locale="en",
        deps_type=_NarrowDeps,
        output_type=router_output_type(default_sport_registry()),
        model_settings_key="router_model",
    )
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    registry.register(narrowed)

    with pytest.raises(PromptTemplateVariableError):
        registry.build_agent("broken", 1)


def test_a_typo_in_the_real_router_template_fails_the_build(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    shutil.copytree(_PROMPTS_ROOT, root)
    body = root / ROUTER_TEMPLATE_ID / "v1.en.md"
    body.write_text(body.read_text().replace("{{message}}", "{{mesage}}"))
    registry = PromptRegistry(root=root)
    registry.register(router_template(default_sport_registry()))

    with pytest.raises(PromptTemplateVariableError):
        registry.build_agent(ROUTER_TEMPLATE_ID, 1)


def test_the_router_template_reads_its_model_from_the_router_model_setting() -> None:
    assert router_template(default_sport_registry()).model_settings_key == "router_model"


async def test_the_rendered_instructions_carry_history_sports_and_time() -> None:
    registry = PromptRegistry(root=_PROMPTS_ROOT)
    registry.register(router_template(default_sport_registry()))
    agent = registry.build_agent(ROUTER_TEMPLATE_ID, 1)
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[0].instructions))  # type: ignore[union-attr]
        return ModelResponse(parts=[TextPart("{}")])

    deps = _deps(
        history=[{"role": "user", "text": "Went bouldering"}],
        athlete_sports=["climbing", "gym"],
    )
    with pytest.raises(Exception):  # noqa: B017, PT011 -- the scripted reply is not valid output
        await agent.run(deps.message, deps=deps, model=FunctionModel(respond))

    rendered = seen[0]
    for expected in ("Went bouldering", "climbing", "Europe/Warsaw", "2026-09-30"):
        assert expected in rendered


def test_recent_turns_keeps_the_last_n_in_order() -> None:
    turns = [ChatTurn(role="user", text=str(index)) for index in range(15)]

    assert [turn.text for turn in recent_turns(turns, limit=10)] == [str(i) for i in range(5, 15)]
    assert recent_turns(turns[:3], limit=10) == turns[:3]


def test_build_router_deps_resolves_today_and_now_local_in_the_user_timezone() -> None:
    now = datetime(2026, 9, 30, 22, 30, tzinfo=UTC)  # already 00:30 on 1 Oct in Warsaw

    deps = build_router_deps(
        message="hi",
        history=[ChatTurn(role="user", text=str(index)) for index in range(12)],
        athlete_sports=["gym"],
        now=now,
        timezone="Europe/Warsaw",
        history_turns=10,
    )

    assert deps.today.isoformat() == "2026-10-01"
    assert deps.now_local.isoformat() == "2026-10-01T00:30:00+02:00"
    assert deps.timezone == "Europe/Warsaw"
    assert [turn.text for turn in deps.history] == [str(index) for index in range(2, 12)]
    assert deps.athlete_sports == ["gym"]
