"""The `chitchat` template: persona replies to small talk, unclear and pain messages
(ADR-0008, ticket #81)."""

import re
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.domain.goals import Goal
from ai_trainer.llm.chitchat import (
    CHITCHAT_TEMPLATE_ID,
    ChitchatDeps,
    build_chitchat_deps,
    chitchat_template,
)
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplateVariableError
from ai_trainer.llm.router import ChatTurn

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"

# 23:30 UTC on 30 Sep is already 01:30 on 1 Oct in Warsaw (ADR-0014).
_NOW = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)


def _registry(root: Path = _PROMPTS_ROOT) -> PromptRegistry:
    registry = PromptRegistry(root=root)
    registry.register(chitchat_template())
    return registry


def _deps(**overrides: object) -> ChitchatDeps:
    values: dict[str, object] = {
        "message": "hi!",
        "history": [ChatTurn(role="user", text=f"turn {index}") for index in range(12)],
        "athlete_sports": ["climbing", "cycling"],
        "goals": [
            Goal(text="Send my first 7a", target_date=date(2027, 6, 1), sport_id="climbing"),
            Goal(text="Stay injury free"),
        ],
        "now": _NOW,
        "timezone": "Europe/Warsaw",
        "history_turns": 10,
    }
    return build_chitchat_deps(**(values | overrides))  # type: ignore[arg-type]


async def _rendered_instructions(deps: ChitchatDeps) -> str:
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[0].instructions))  # type: ignore[union-attr]
        return ModelResponse(parts=[TextPart("Hey! Good to hear from you.")])

    agent = _registry().build_agent(CHITCHAT_TEMPLATE_ID, 1)
    await agent.run(deps.message, deps=deps, model=FunctionModel(respond))
    return seen[0]


def test_the_chitchat_template_is_v1_with_text_output_and_its_own_model_setting() -> None:
    template = chitchat_template()

    assert (template.id, template.version, template.locale) == ("chitchat", 1, "en")
    assert template.deps_type is ChitchatDeps
    assert template.output_type is str
    assert template.model_settings_key == "chitchat_model"


async def test_the_agent_replies_with_plain_text() -> None:
    agent = _registry().build_agent(CHITCHAT_TEMPLATE_ID, 1)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Hey! Good to hear from you.")])

    result = await agent.run("hi!", deps=_deps(), model=FunctionModel(respond))

    assert result.output == "Hey! Good to hear from you."


async def test_the_rendered_prompt_carries_the_recent_turns_only() -> None:
    rendered = await _rendered_instructions(_deps())

    turns = re.findall(r"^- user: (turn \d+)$", rendered, flags=re.MULTILINE)

    assert turns == [f"turn {index}" for index in range(2, 12)]


async def test_the_rendered_prompt_carries_the_athletes_local_date_not_the_servers() -> None:
    rendered = await _rendered_instructions(_deps())

    assert "2026-10-01" in rendered
    assert "01:30" in rendered
    assert "Europe/Warsaw" in rendered
    assert "2026-09-30" not in rendered


async def test_the_rendered_prompt_carries_the_message_sports_and_goals() -> None:
    rendered = await _rendered_instructions(_deps(message="Morning, coach!"))

    for expected in ("Morning, coach!", "climbing", "cycling", "Send my first 7a", "2027-06-01"):
        assert expected in rendered
    assert "Stay injury free" in rendered


async def test_the_persona_comes_before_the_chitchat_instructions() -> None:
    rendered = await _rendered_instructions(_deps())

    assert rendered.index("training companion") < rendered.index("Recent messages")


async def test_an_athlete_with_no_sports_or_goals_still_renders() -> None:
    rendered = await _rendered_instructions(_deps(athlete_sports=[], goals=[], history=[]))

    assert "none set yet" in rendered


def test_a_typo_in_the_real_chitchat_template_fails_the_build(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    shutil.copytree(_PROMPTS_ROOT, root)
    body = root / CHITCHAT_TEMPLATE_ID / "v1.en.md"
    body.write_text(body.read_text().replace("{{message}}", "{{mesage}}"))

    with pytest.raises(PromptTemplateVariableError):
        _registry(root).build_agent(CHITCHAT_TEMPLATE_ID, 1)


def test_build_chitchat_deps_resolves_today_and_now_local_in_the_user_timezone() -> None:
    deps = _deps()

    assert deps.today.isoformat() == "2026-10-01"
    assert deps.now_local.isoformat() == "2026-10-01T01:30:00+02:00"
    assert deps.timezone == "Europe/Warsaw"
    assert [turn.text for turn in deps.history] == [f"turn {index}" for index in range(2, 12)]
