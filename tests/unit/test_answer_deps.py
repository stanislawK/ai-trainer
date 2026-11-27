"""What an answer template knows of the athlete beyond the message (PRD 0013 B46, ADR-0028, ticket
#629): their goals in the answered sport or no sport, with each goal's kind, and their limits.
Nothing identifying: no ID, no target date, no name."""

from datetime import UTC, date, datetime
from pathlib import Path

from ai_trainer.domain.goals import Goal, GoalKind
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.answer import AnswerDeps, AnswerGoal, build_answer_deps
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.router import ChatTurn

_PROMPTS_ROOT = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "prompts"
_NOW = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)


def _deps(goals: list[Goal], limits: str | None, sport_id: str = "gym") -> AnswerDeps:
    return build_answer_deps(
        message="What should I focus on?",
        history=[],
        now=_NOW,
        timezone="Europe/Warsaw",
        history_turns=10,
        goals=goals,
        limits=limits,
        sport_id=sport_id,
    )


def _shared_prefix(first: str, second: str) -> str:
    size = next(
        (index for index, (a, b) in enumerate(zip(first, second, strict=False)) if a != b),
        min(len(first), len(second)),
    )
    return first[:size]


def _render(deps: AnswerDeps) -> str:
    registry = build_prompt_registry(_PROMPTS_ROOT, default_sport_registry())
    return registry.render_instructions("gym.answer", 4, deps)


def test_answer_deps_carry_the_sports_goals_with_kind() -> None:
    goals = [
        Goal(text="Climb 7a", sport_id="climbing", kind=GoalKind.STRENGTH),
        Goal(
            text="Touch my toes",
            target_date=date(2027, 3, 1),
            sport_id="gym",
            kind=GoalKind.MOBILITY,
        ),
        Goal(text="Feel fitter", kind=None),
    ]

    deps = _deps(goals, "sore left shoulder")

    assert deps.goals == [
        AnswerGoal(text="Touch my toes", kind=GoalKind.MOBILITY),
        AnswerGoal(text="Feel fitter", kind=None),
    ]
    assert deps.limits == "sore left shoulder"
    assert "target_date" not in AnswerGoal.model_fields
    assert "sport_id" not in AnswerGoal.model_fields


def test_gym_answer_v4_renders_goals_and_limits() -> None:
    deps = _deps(
        [
            Goal(text="Touch my toes", sport_id="gym", kind=GoalKind.MOBILITY),
            Goal(text="Feel fitter"),
        ],
        "sore left shoulder",
    )

    instructions = _render(deps)

    assert "## About the athlete" in instructions
    assert "Touch my toes (mobility)" in instructions
    assert "Feel fitter" in instructions
    assert "Feel fitter (" not in instructions
    assert "sore left shoulder" in instructions
    assert "find_exercise" in instructions
    assert "doctor or physiotherapist" in instructions


def test_no_goals_and_no_profile_render_no_athlete_block() -> None:
    deps = _deps([Goal(text="Climb 7a", sport_id="climbing")], None)

    instructions = _render(deps)

    assert deps.goals == []
    assert "About the athlete" not in instructions
    assert "Goals:" not in instructions
    assert "Limits:" not in instructions
    assert "Climb 7a" not in instructions
    assert "today is 2026-11-24" in instructions


def test_gym_answer_v4_renders_limits_alone() -> None:
    instructions = _render(_deps([], "bad knee"))

    assert "## About the athlete" in instructions
    assert "Limits: bad knee" in instructions
    assert "Goals:" not in instructions


def test_gym_answer_v4_renders_share_the_static_prefix_up_to_the_first_athlete_value() -> None:
    """ADR-0026 step 6 (#663): the task and every rule come before the turn's context, so two
    athletes on different days share every character up to the first athlete value."""
    first = build_answer_deps(
        message="How is my bench going?",
        history=[ChatTurn(role="user", text="hi")],
        now=_NOW,
        timezone="Europe/Warsaw",
        history_turns=10,
        goals=[Goal(text="Touch my toes", sport_id="gym", kind=GoalKind.MOBILITY)],
        limits="sore left shoulder",
        sport_id="gym",
    )
    second = build_answer_deps(
        message="How often should I squat?",
        history=[ChatTurn(role="assistant", text="noted")],
        now=datetime(2027, 2, 3, 6, 15, tzinfo=UTC),
        timezone="America/New_York",
        history_turns=10,
        goals=[Goal(text="Run 5 km", kind=GoalKind.ENDURANCE)],
        limits="bad knee",
        sport_id="gym",
    )

    prefix = _shared_prefix(_render(first), _render(second))

    assert prefix.endswith("## About the athlete\nGoals: ")
    assert "## Rules" in prefix
    assert "\n11. " in prefix


def test_gym_answer_v4_leaves_the_latest_message_out_of_the_instructions() -> None:
    message = "Zebra-crossing deadlift question 4711"
    deps = build_answer_deps(
        message=message,
        history=[],
        now=_NOW,
        timezone="Europe/Warsaw",
        history_turns=10,
        goals=[],
        limits=None,
        sport_id="gym",
    )

    instructions = _render(deps)

    assert message not in instructions
    assert "## Latest message" not in instructions


def test_gym_answer_v4_with_no_goals_or_limits_renders_no_athlete_heading() -> None:
    instructions = _render(_deps([], None))

    assert "About the athlete" not in instructions
    assert "Goals:" not in instructions
    assert "Limits:" not in instructions
    assert "today is 2026-11-24" in instructions
