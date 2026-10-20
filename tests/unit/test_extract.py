"""Sport extraction templates: deps, the plugin-named template and its code-graded evaluators
(ADR-0006, ADR-0008, ADR-0014, tickets #153, #159 and #160)."""

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, UnexpectedModelBehavior
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.evaluators import EvaluatorContext

from ai_trainer.domain.sports.base import ExtractionSpec, SessionExtraction, SportPlugin
from ai_trainer.domain.sports.climbing.edit import apply_climbing_edit
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction
from ai_trainer.domain.sports.climbing.payload import (
    CLIMBING_PAYLOAD_VERSION,
    ClimbingPayload,
    Grade,
)
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.extract import (
    ActivitiesMatch,
    DateMatch,
    ExtractDeps,
    build_extract_deps,
    extract_template,
    extract_templates,
)
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate, PromptTemplateVariableError
from ai_trainer.llm.router import ChatTurn

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
_CLIMBING_EXTRACT = "climbing.extract"

# 22:30 UTC on 15 Sep is already 00:30 on Wednesday 16 Sep in Warsaw (ADR-0014).
_NOW = datetime(2026, 9, 15, 22, 30, tzinfo=UTC)


def _deps(**overrides: Any) -> ExtractDeps:
    values: dict[str, Any] = {
        "message": "I did 3 6b+ yesterday",
        "history": [ChatTurn(role="user", text=f"turn {index}") for index in range(12)],
        "now": _NOW,
        "timezone": "Europe/Warsaw",
        "history_turns": 10,
    }
    return build_extract_deps(**(values | overrides))


def _registry(root: Path = _PROMPTS_ROOT) -> PromptRegistry:
    return build_prompt_registry(root, default_sport_registry())


def _extraction_response(args: dict[str, Any]) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return FunctionModel(respond)


async def _rendered_instructions(deps: ExtractDeps) -> str:
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[0].instructions))  # type: ignore[union-attr]
        args = {
            "occurred_on": "2026-09-15",
            "date_source": "relative",
            "activities": [{"kind": "lead", "count": 3, "grade": "6b+"}],
        }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    agent = _registry().build_agent(_CLIMBING_EXTRACT, 1)
    await agent.run(deps.message, deps=deps, model=FunctionModel(respond))
    return seen[0]


def test_build_extract_deps_resolves_today_weekday_and_now_in_the_athletes_timezone() -> None:
    deps = _deps()

    assert deps.today.isoformat() == "2026-09-16"
    assert deps.weekday == "Wednesday"
    assert deps.now_local.isoformat() == "2026-09-16T00:30:00+02:00"
    assert deps.timezone == "Europe/Warsaw"
    assert [turn.text for turn in deps.history] == [f"turn {index}" for index in range(2, 12)]


def test_build_extract_deps_crosses_a_dst_change_in_local_time() -> None:
    # 25 Oct 2026, 01:30 UTC: Warsaw is back on CET (+01:00) at 02:30 local.
    deps = _deps(now=datetime(2026, 10, 25, 1, 30, tzinfo=UTC))

    assert deps.now_local.isoformat() == "2026-10-25T02:30:00+01:00"
    assert deps.weekday == "Sunday"


def test_the_climbing_plugin_names_its_extraction_template() -> None:
    climbing = default_sport_registry().get("climbing")

    assert climbing.extraction == ExtractionSpec(
        template_id=_CLIMBING_EXTRACT,
        version=2,
        output_type=ClimbingExtraction,
        payload_type=ClimbingPayload,
        payload_version=CLIMBING_PAYLOAD_VERSION,
        apply_edit=apply_climbing_edit,
    )


def test_extract_template_takes_its_id_version_and_output_from_the_plugin() -> None:
    spec = ExtractionSpec(
        template_id=_CLIMBING_EXTRACT,
        version=1,
        output_type=ClimbingExtraction,
        payload_type=ClimbingPayload,
        payload_version=1,
        apply_edit=apply_climbing_edit,
    )
    template = extract_template(spec)

    assert (template.id, template.version, template.locale) == (_CLIMBING_EXTRACT, 1, "en")
    assert template.deps_type is ExtractDeps
    assert template.output_type is ClimbingExtraction
    assert template.model_settings_key == "extract_model"
    assert template.provider_settings_key == "extract_provider_preferences"
    assert template.reasoning_effort_settings_key == "extract_reasoning_effort"
    assert template.temperature == 0
    assert template.output_retries == 2


def test_extract_templates_skip_a_sport_without_an_extraction() -> None:
    sports = SportRegistry(
        [
            *default_sport_registry().all(),
            SportPlugin(id="rowing", label_key="sport.rowing.label", icon="waves"),
        ]
    )

    assert [(template.id, template.version) for template in extract_templates(sports)] == [
        (_CLIMBING_EXTRACT, 1),
        (_CLIMBING_EXTRACT, 2),
        ("gym.extract", 1),
        ("gym.extract", 2),
        ("cycling.extract", 1),
        ("cycling.extract", 2),
    ]


def test_the_app_registry_registers_climbing_extract() -> None:
    template = _registry().get(_CLIMBING_EXTRACT, 1)

    assert template.output_type is ClimbingExtraction


async def test_the_climbing_agent_returns_an_extraction_that_becomes_a_font_payload() -> None:
    agent = _registry().build_agent(_CLIMBING_EXTRACT, 1)
    model = _extraction_response(
        {
            "occurred_on": "2026-09-16",
            "date_source": "assumed_today",
            "activities": [
                {"kind": "boulder", "count": 3, "grade": "6A"},
                {"kind": "boulder", "count": 2, "grade": "6B"},
            ],
        }
    )

    result = await agent.run("I climbed 3 6A and 2 6B boulders", deps=_deps(), model=model)

    assert isinstance(result.output, ClimbingExtraction)
    payload = result.output.to_payload()
    grades = [(a.count, a.grade) for a in payload.activities]
    assert grades == [
        (3, Grade(original="6A", scale="font")),
        (2, Grade(original="6B", scale="font")),
    ]


async def test_the_cycling_agent_returns_a_ride_in_miles_that_becomes_km() -> None:
    agent = _registry().build_agent("cycling.extract", 1)
    model = _extraction_response(
        {
            "occurred_on": "2026-09-15",
            "date_source": "relative",
            "activities": [{"distance": {"original": "40 miles", "value": 40, "unit": "mi"}}],
        }
    )

    result = await agent.run("Rode 40 miles yesterday", deps=_deps(), model=model)

    assert isinstance(result.output, CyclingExtraction)
    (ride,) = result.output.to_payload().activities
    assert ride.distance is not None
    assert (ride.distance.original, ride.distance.km) == ("40 miles", 64.37)


async def test_the_cycling_prompt_carries_the_local_day_and_the_message() -> None:
    deps = _deps(message="Rode 62 km with 800 m climbing this morning")

    rendered = _registry().render_instructions("cycling.extract", 1, deps)

    assert "Wednesday 2026-09-16" in rendered
    assert "Rode 62 km with 800 m climbing this morning" in rendered
    assert "cycling log" in rendered


async def test_the_gym_agent_returns_a_load_in_lb_that_becomes_kg() -> None:
    agent = _registry().build_agent("gym.extract", 1)
    model = _extraction_response(
        {
            "occurred_on": "2026-09-15",
            "date_source": "relative",
            "activities": [
                {
                    "name": "bench",
                    "sets": 3,
                    "reps": 8,
                    "load": {"original": "135 lb", "value": 135, "unit": "lb"},
                }
            ],
        }
    )

    result = await agent.run("Bench yesterday 3x8 @ 135 lb", deps=_deps(), model=model)

    assert isinstance(result.output, GymExtraction)
    (exercise,) = result.output.to_payload().activities
    assert exercise.load is not None
    assert (exercise.load.original, exercise.load.kg) == ("135 lb", 61.23)


async def test_the_gym_prompt_carries_the_local_day_and_the_message() -> None:
    deps = _deps(message="Bench yesterday 3\N{MULTIPLICATION SIGN}8 @ 60")

    rendered = _registry().render_instructions("gym.extract", 1, deps)

    assert "Wednesday 2026-09-16" in rendered
    assert "Bench yesterday 3\N{MULTIPLICATION SIGN}8 @ 60" in rendered
    assert "gym log" in rendered


async def test_the_rendered_prompt_carries_the_athletes_local_day_and_weekday() -> None:
    rendered = await _rendered_instructions(_deps())

    assert "2026-09-16" in rendered
    assert "Wednesday" in rendered
    assert "Europe/Warsaw" in rendered
    assert "2026-09-15" not in rendered


async def test_the_rendered_prompt_carries_the_message_and_the_recent_turns_only() -> None:
    rendered = await _rendered_instructions(_deps(message="sent 2 V4 on Tuesday"))

    assert "sent 2 V4 on Tuesday" in rendered
    assert "turn 1\n" not in rendered
    assert "turn 2" in rendered
    assert "turn 11" in rendered


def test_a_typo_in_the_climbing_extract_template_fails_the_build(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    shutil.copytree(_PROMPTS_ROOT, root)
    body = root / _CLIMBING_EXTRACT / "v1.en.md"
    body.write_text(body.read_text().replace("{{message}}", "{{mesage}}"))

    with pytest.raises(PromptTemplateVariableError):
        _registry(root).build_agent(_CLIMBING_EXTRACT, 1)


def _extraction(**overrides: Any) -> ClimbingExtraction:
    values: dict[str, Any] = {
        "occurred_on": "2026-09-15",
        "date_source": "relative",
        "activities": [
            {"kind": "boulder", "count": 3, "grade": "6A"},
            {"kind": "boulder", "count": 2, "grade": "6B"},
        ],
    }
    return ClimbingExtraction.model_validate(values | overrides)


def _ctx(
    output: BaseModel, expected: BaseModel, metadata: dict[str, Any] | None = None
) -> EvaluatorContext[EvalCaseInputs, BaseModel]:
    return EvaluatorContext(
        name="case",
        inputs=EvalCaseInputs(prompt="x"),
        metadata=metadata,
        expected_output=expected,
        output=output,
        duration=0.0,
        _span_tree=None,  # type: ignore[arg-type]
        attributes={},
        metrics={},
    )


def test_date_match_needs_the_same_day_and_date_source() -> None:
    expected = _extraction()

    assert DateMatch().evaluate(_ctx(_extraction(), expected)) is True
    assert DateMatch().evaluate(_ctx(_extraction(occurred_on="2026-09-16"), expected)) is False
    assert DateMatch().evaluate(_ctx(_extraction(date_source="stated"), expected)) is False


def test_activities_match_ignores_order_but_not_counts_or_grades() -> None:
    expected = _extraction()
    reordered = _extraction(
        activities=[
            {"kind": "boulder", "count": 2, "grade": "6B"},
            {"kind": "boulder", "count": 3, "grade": "6A"},
        ]
    )
    wrong_count = _extraction(
        activities=[
            {"kind": "boulder", "count": 2, "grade": "6A"},
            {"kind": "boulder", "count": 2, "grade": "6B"},
        ]
    )
    rescaled = _extraction(
        activities=[
            {"kind": "boulder", "count": 3, "grade": "6a"},
            {"kind": "boulder", "count": 2, "grade": "6b"},
        ]
    )

    assert ActivitiesMatch().evaluate(_ctx(reordered, expected)) is True
    assert ActivitiesMatch().evaluate(_ctx(wrong_count, expected)) is False
    assert ActivitiesMatch().evaluate(_ctx(rescaled, expected)) is False


def test_activities_match_fails_an_invented_grade_or_a_missing_activity() -> None:
    expected = _extraction(activities=[{"kind": "boulder", "count": 5, "grade": None}])
    invented = _extraction(activities=[{"kind": "boulder", "count": 5, "grade": "6A"}])
    extra = _extraction(
        activities=[
            {"kind": "boulder", "count": 5, "grade": None},
            {"kind": "lead", "count": 1, "grade": None},
        ]
    )

    assert ActivitiesMatch().evaluate(_ctx(invented, expected)) is False
    assert ActivitiesMatch().evaluate(_ctx(extra, expected)) is False


def test_activities_match_skips_the_fields_a_case_lists_as_ignored() -> None:
    expected = _extraction(activities=[{"kind": "lead", "count": 3, "grade": "6b+"}])
    top_rope = _extraction(
        activities=[{"kind": "top_rope", "count": 3, "grade": "6b+", "style": "redpoint"}]
    )

    assert ActivitiesMatch().evaluate(_ctx(top_rope, expected)) is False
    assert (
        ActivitiesMatch().evaluate(_ctx(top_rope, expected, {"ignore_fields": ["kind", "style"]}))
        is True
    )


def test_an_extraction_base_names_no_payload_until_a_sport_defines_one() -> None:
    bare = SessionExtraction(occurred_on=date(2026, 10, 2), date_source="stated")

    with pytest.raises(NotImplementedError):
        bare.to_payload()


_ACTIVE_EXTRACT_TEMPLATES = [
    extract_template(plugin.extraction)
    for plugin in default_sport_registry().all()
    if plugin.extraction is not None
]
_PERSONA_LINE = "You are the athlete's training companion"


def _render_active(template_id: str, version: int, deps: ExtractDeps) -> str:
    return _registry().render_instructions(template_id, version, deps)


def test_every_sport_runs_extraction_v2() -> None:
    assert {(template.id, template.version) for template in _ACTIVE_EXTRACT_TEMPLATES} == {
        ("climbing.extract", 2),
        ("gym.extract", 2),
        ("cycling.extract", 2),
    }


def test_every_extraction_version_stays_registered_for_evals() -> None:
    registry = _registry()

    for template in _ACTIVE_EXTRACT_TEMPLATES:
        for version in range(1, template.version + 1):
            assert registry.get(template.id, version).version == version


@pytest.mark.parametrize("template", _ACTIVE_EXTRACT_TEMPLATES, ids=lambda t: t.id)
async def test_extraction_instructions_carry_no_persona(
    template: PromptTemplate[ExtractDeps, BaseModel],
) -> None:
    """Extraction never writes to the athlete, so it does without the persona, in the app and
    in evals alike (ADR-0026 step 6). Chitchat keeps it (`test_chitchat.py`)."""
    deps = _deps()
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[0].instructions))  # type: ignore[union-attr]
        return ModelResponse(parts=[TextPart("{}")])

    agent = _registry().build_agent(template.id, template.version)
    with pytest.raises(UnexpectedModelBehavior):
        await agent.run(deps.message, deps=deps, model=FunctionModel(respond))

    assert template.persona is False
    assert _PERSONA_LINE not in seen[0]
    assert _PERSONA_LINE not in _render_active(template.id, template.version, deps)
    assert _PERSONA_LINE in _registry().load_persona()


def test_extraction_history_holds_only_the_athletes_own_messages() -> None:
    history = [
        ChatTurn(role="user", text="Did 3 \N{MULTIPLICATION SIGN} 6b today"),
        ChatTurn(role="assistant", text="Check it before it's saved."),
        ChatTurn(role="user", text="also a 6c"),
    ]

    deps = _deps(message="and 2 more of those", history=history)

    assert deps.history == [history[0], history[2]]


def test_eval_deps_drop_assistant_lines_too() -> None:
    """Eval datasets pass deps as written, so the deps model itself keeps the athlete's turns:
    evals read what the app reads."""
    deps = ExtractDeps.model_validate(
        {
            "message": "and 2 more of those",
            "history": [
                {"role": "user", "text": "3 \N{MULTIPLICATION SIGN} 6b"},
                {"role": "assistant", "text": "Nice! Here's your draft."},
            ],
            "today": "2026-09-16",
            "weekday": "Wednesday",
            "now_local": "2026-09-16T19:00:00+02:00",
            "timezone": "Europe/Warsaw",
        }
    )

    assert deps.history == [ChatTurn(role="user", text="3 \N{MULTIPLICATION SIGN} 6b")]


@pytest.mark.parametrize("template", _ACTIVE_EXTRACT_TEMPLATES, ids=lambda t: t.id)
def test_rendered_extraction_history_contains_no_assistant_line(
    template: PromptTemplate[ExtractDeps, BaseModel],
) -> None:
    history = [
        ChatTurn(role="user", text="Did 3 \N{MULTIPLICATION SIGN} 6b today"),
        ChatTurn(role="assistant", text="Check it before it's saved."),
    ]

    rendered = _render_active(
        template.id, template.version, _deps(message="and 2 more", history=history)
    )

    assert "Did 3 \N{MULTIPLICATION SIGN} 6b today" in rendered
    assert "assistant" not in rendered
    assert "Check it before it's saved." not in rendered


@pytest.mark.parametrize("template", _ACTIVE_EXTRACT_TEMPLATES, ids=lambda t: t.id)
def test_two_renders_a_minute_apart_share_the_static_prefix(
    template: PromptTemplate[ExtractDeps, BaseModel],
) -> None:
    """Every rule comes before the first dynamic part, so the static prefix can be cached
    (ADR-0026 step 6); the date, time, history and message come last."""
    first = _render_active(template.id, template.version, _deps(now=_NOW))
    second = _render_active(
        template.id,
        template.version,
        _deps(now=_NOW + timedelta(minutes=1), message="and 2 more", history=[]),
    )

    dynamic_start = first.index("Today is")
    assert second.index("Today is") == dynamic_start
    assert first[:dynamic_start] == second[:dynamic_start]
    assert first[dynamic_start:] != second[dynamic_start:]
    static = first[:dynamic_start]
    for heading in ("## Date", "## Activities"):
        assert heading in static
    assert "turn 11" in first[dynamic_start:]
    assert "I did 3 6b+ yesterday" in first[dynamic_start:]
