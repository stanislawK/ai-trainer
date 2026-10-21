"""The committed `climbing.extract` eval dataset covers the ticket's cases (ADR-0009, tickets
#153 and #164)."""

from datetime import date
from pathlib import Path
from typing import Any

from pydantic_evals import Dataset

from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction, ClimbingQuestion
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.extract import ActivitiesMatch, DateMatch, ExtractDeps, QuestionMatch

_ROOT = Path(__file__).parents[2] / "evals"
_DATASET = _ROOT / "datasets" / "climbing.extract.yaml"
_BASELINE = _ROOT / "baselines" / "climbing.extract.json"


def _dataset() -> Dataset[EvalCaseInputs, ClimbingExtraction | ClimbingQuestion, Any]:
    return Dataset[EvalCaseInputs, ClimbingExtraction | ClimbingQuestion, Any].from_file(
        _DATASET, custom_evaluator_types=(DateMatch, ActivitiesMatch, QuestionMatch)
    )


def _cases() -> dict[str, Any]:
    return {str(case.name): case for case in _dataset().cases}


def _expected(name: str) -> ClimbingExtraction:
    expected = _cases()[name].expected_output
    assert isinstance(expected, ClimbingExtraction)
    return expected


def test_the_dataset_has_at_least_twenty_cases_graded_in_code() -> None:
    dataset = _dataset()

    assert len(dataset.cases) >= 20
    assert {type(evaluator) for evaluator in dataset.evaluators} == {
        DateMatch,
        ActivitiesMatch,
        QuestionMatch,
    }
    assert all(case.expected_output is not None for case in dataset.cases)


def test_every_case_carries_valid_deps_with_its_message() -> None:
    for case in _dataset().cases:
        deps = ExtractDeps.model_validate(case.inputs.deps)
        assert deps.message == case.inputs.prompt, case.name
        assert deps.now_local.date() == deps.today, case.name
        assert deps.now_local.strftime("%A") == deps.weekday, case.name


def test_the_dataset_covers_relative_dates_several_activities_and_every_scale() -> None:
    names = " ".join(_cases())

    for kind in ("relative:", "stated:", "assumed:", "multi-activity:", "scale:", "mixed sport:"):
        assert kind in names, kind
    for scale in ("font", "french", "v-scale", "yds", "unrecognised"):
        assert scale in names, scale


def test_three_6a_and_two_6b_boulders_are_two_font_activities() -> None:
    payload = _expected("multi-activity: font boulders (AC)").to_payload()

    assert [(a.kind, a.count, a.grade) for a in payload.activities] == [
        ("boulder", 3, payload.activities[0].grade),
        ("boulder", 2, payload.activities[1].grade),
    ]
    assert [(a.grade.original, a.grade.scale) for a in payload.activities if a.grade] == [
        ("6A", "font"),
        ("6B", "font"),
    ]


def test_yesterday_from_warsaw_on_16_september_is_the_15th_and_relative() -> None:
    case = _cases()["relative: yesterday from Warsaw (AC)"]
    expected = _expected("relative: yesterday from Warsaw (AC)")

    assert case.inputs.prompt == "I did 3 6b+ yesterday"
    assert case.inputs.deps["today"] == "2026-09-16"
    assert case.inputs.deps["timezone"] == "Europe/Warsaw"
    assert (expected.occurred_on, expected.date_source) == (date(2026, 9, 15), "relative")


def test_a_message_without_a_date_is_today_and_assumed() -> None:
    case = _cases()["assumed: no date given (AC)"]
    expected = _expected("assumed: no date given (AC)")

    assert expected.occurred_on.isoformat() == case.inputs.deps["today"]
    assert expected.date_source == "assumed_today"


def test_an_unrecognised_grade_is_kept_as_typed_with_no_scale() -> None:
    name = "scale: unrecognised colour grade (AC)"
    payload = _expected(name).to_payload()

    grades = [activity.grade for activity in payload.activities if activity.grade is not None]
    assert len(grades) == len(payload.activities)
    assert [grade.scale for grade in grades] == [None] * len(grades)
    assert all(grade.original in _cases()[name].inputs.prompt for grade in grades)


def test_a_log_without_a_grade_expects_no_invented_grade() -> None:
    expected = _expected("no grade: none invented")

    assert all(activity.grade is None for activity in expected.activities)


def test_the_baseline_is_committed() -> None:
    assert _BASELINE.is_file()


def _reading_cases() -> dict[str, Any]:
    return {name: case for name, case in _cases().items() if name.startswith("reading:")}


def test_a_bare_7a_from_a_boulderer_who_leads_expects_the_two_readings() -> None:
    case = _cases()["reading: boulders and leads, a bare 7a asks (AC)"]

    assert case.inputs.deps["recent"] == ["boulder", "lead"]
    assert case.expected_output == ClimbingQuestion.model_validate(
        {"readings": [{"kind": "boulder", "grade": "7a"}, {"kind": "lead", "grade": "7a"}]}
    )


def test_the_same_log_from_a_lead_only_athlete_expects_a_lead() -> None:
    case = _cases()["reading: lead-only athlete, a bare 7a drafts a lead (AC)"]

    assert case.inputs.prompt == "Did 3 7a"
    assert case.inputs.deps["recent"] == ["lead"]
    assert [a.kind for a in _expected(case.name).activities] == ["lead"]


def test_the_reading_cases_ask_less_often_than_they_draft() -> None:
    """Asking has a cost: most reading cases are logs that something already settles."""
    cases = _reading_cases()
    asks = [
        name for name, case in cases.items() if isinstance(case.expected_output, ClimbingQuestion)
    ]

    assert len(cases) >= 10
    assert 0 < len(asks) < len(cases) / 2


def test_an_answered_case_always_expects_a_draft_with_the_answered_kind() -> None:
    """The answer reads the climbs the log left open; a climb it settled keeps its kind."""
    answered = [case for case in _reading_cases().values() if case.inputs.deps.get("answers")]

    assert len(answered) >= 3
    for case in answered:
        [kind] = case.inputs.deps["answers"]
        assert kind in {a.kind for a in _expected(case.name).activities}, case.name


def test_the_cases_before_the_question_carry_no_recent_kinds_and_still_draft() -> None:
    """A clear log in the dataset still drafts, with no question (ticket #164)."""
    for name, case in _cases().items():
        if name.startswith("reading:"):
            continue
        assert "recent" not in case.inputs.deps, name
        assert isinstance(case.expected_output, ClimbingExtraction), name
