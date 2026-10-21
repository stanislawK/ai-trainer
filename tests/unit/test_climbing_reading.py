"""The climbing question (B17, ADR-0015, ticket #164): a log `climbing.extract` can't read
safely becomes readings, likeliest first, that code turns into options; and the climbing kinds
of the athlete's recent sessions, computed in code for the extraction's deps."""

from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.clarification import ChoiceOption
from ai_trainer.domain.sports.climbing.extraction import (
    ClimbingExtraction,
    ClimbingQuestion,
    recent_kinds,
)
from ai_trainer.domain.sports.climbing.normalize import grade_in_scale
from ai_trainer.domain.sports.climbing.payload import ClimbingKind, Grade
from ai_trainer.domain.sports.registry import default_sport_registry


def _question(*readings: tuple[str, str]) -> ClimbingQuestion:
    return ClimbingQuestion.model_validate(
        {"readings": [{"kind": kind, "grade": grade} for kind, grade in readings]}
    )


def test_each_reading_becomes_an_option_with_its_kind_and_the_grade_in_its_scale() -> None:
    question = _question(("boulder", "7a"), ("lead", "7a"))

    assert question.options() == (
        ChoiceOption(id="boulder", copy_key="climbing_reading_boulder", params={"grade": "7A"}),
        ChoiceOption(id="lead", copy_key="climbing_reading_lead", params={"grade": "7a"}),
    )


def test_options_keep_the_readings_order_likeliest_first() -> None:
    question = _question(("top_rope", "6B+"), ("boulder", "6B+"), ("lead", "6B+"))

    assert [option.id for option in question.options()] == ["top_rope", "boulder", "lead"]
    assert [option.params["grade"] for option in question.options()] == ["6b+", "6B+", "6b+"]


@pytest.mark.parametrize(
    "readings",
    [
        [{"kind": "boulder", "grade": "7a"}],
        [{"kind": "boulder", "grade": "7a"}, {"kind": "boulder", "grade": "7A"}],
        [{"kind": "board", "grade": "7a"}, {"kind": "lead", "grade": "7a"}],
    ],
)
def test_a_question_needs_two_readings_of_distinct_kinds(readings: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        ClimbingQuestion.model_validate({"readings": readings})


@pytest.mark.parametrize(
    ("original", "kind", "shown"),
    [
        ("7a", "boulder", "7A"),
        ("7A+", "lead", "7a+"),
        ("6c", "top_rope", "6c"),
        ("6C", "boulder", "6C"),
        # A grade that is neither Font nor French says its own scale, so it is left as typed.
        ("V5", "boulder", "V5"),
        ("5.11a", "lead", "5.11a"),
        ("blue", "boulder", "blue"),
        (" 7a ", "boulder", "7A"),
    ],
)
def test_a_reading_shows_its_grade_in_the_scale_of_its_kind(
    original: str, kind: ClimbingKind, shown: str
) -> None:
    assert grade_in_scale(original, kind) == shown


def _payload(*kinds: str) -> dict[str, Any]:
    return {
        "sport": "climbing",
        "activities": [{"kind": kind, "count": 1, "grade": None} for kind in kinds],
    }


def test_recent_kinds_are_each_kind_once_most_recent_session_first() -> None:
    assert recent_kinds([_payload("lead"), _payload("boulder", "lead"), _payload("top_rope")]) == (
        "lead",
        "boulder",
        "top_rope",
    )


def test_no_recent_sessions_give_no_kinds() -> None:
    assert recent_kinds([]) == ()


def test_the_climbing_plugin_asks_and_reads_recent_kinds() -> None:
    spec = default_sport_registry().get("climbing").extraction

    assert spec is not None
    assert spec.question is not None
    assert spec.question.type is ClimbingQuestion
    assert spec.question.since_version == 3
    assert spec.version >= spec.question.since_version
    assert spec.recent is recent_kinds


def _log(*climbs: tuple[str, str | None]) -> ClimbingExtraction:
    return ClimbingExtraction.model_validate(
        {
            "occurred_on": "2026-10-02",
            "date_source": "assumed_today",
            "activities": [{"kind": kind, "count": 1, "grade": grade} for kind, grade in climbs],
        }
    )


def _pick(kind: str, grade: str) -> ChoiceOption:
    return ChoiceOption(id=kind, copy_key=f"climbing_reading_{kind}", params={"grade": grade})


def test_an_answer_sets_the_picked_kind_on_the_climbs_at_the_grade_it_asked_about() -> None:
    """The tap is enforced in code, not only in the prompt (skeptic finding at #164)."""
    answered = _log(("boulder", "7a"), ("boulder", "7a")).answered(_pick("lead", "7a"))

    assert [climb.kind for climb in answered.activities] == ["lead", "lead"]
    assert [a.grade for a in answered.to_payload().activities] == [
        Grade(original="7a", scale="french"),
        Grade(original="7a", scale="french"),
    ]


def test_the_grade_shown_in_the_options_scale_matches_the_grade_as_typed() -> None:
    """A boulder option shows "7A" for a typed "7a"; both are the climb it asked about."""
    answered = _log(("lead", "7a")).answered(_pick("boulder", "7A"))

    assert [climb.kind for climb in answered.activities] == ["boulder"]


def test_an_answer_leaves_every_other_climb_as_the_message_said() -> None:
    """ "5 6B boulders, then a 7a" asked about the 7a only (skeptic finding at #164)."""
    answered = _log(("boulder", "6B"), ("lead", "7a"), ("lead", None), ("boulder", "V4")).answered(
        _pick("top_rope", "7a")
    )

    assert [climb.kind for climb in answered.activities] == [
        "boulder",
        "top_rope",
        "lead",
        "boulder",
    ]


@pytest.mark.parametrize(
    "answer",
    [
        ChoiceOption(id="board", copy_key="x", params={"grade": "7a"}),
        ChoiceOption(id="lead", copy_key="x"),
        ChoiceOption(id="climbing", copy_key="sport.climbing.label"),
    ],
    ids=["not a kind", "no grade", "a sport's option"],
)
def test_an_option_that_is_no_reading_changes_nothing(answer: ChoiceOption) -> None:
    log = _log(("boulder", "7a"))

    assert log.answered(answer) == log
