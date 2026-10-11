from pathlib import Path

import pytest

from ai_trainer.llm.evals.baseline import (
    Baseline,
    BaselineThresholds,
    CaseProbability,
    find_changes,
    find_regressions,
    load_baseline,
    save_baseline,
)


def _baseline(**overrides: object) -> Baseline:
    defaults: dict[str, object] = {
        "template_id": "sample",
        "version": 1,
        "model": "openai/gpt-5-mini",
        "assertions": 1.0,
        "scores": {"helpfulness": 0.9},
        "thresholds": BaselineThresholds(),
    }
    defaults.update(overrides)
    return Baseline.model_validate(defaults)


def test_load_baseline_returns_none_when_file_is_missing(tmp_path: Path) -> None:
    assert load_baseline(tmp_path / "sample.json") is None


def test_save_then_load_round_trips_the_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baselines" / "sample.json"
    baseline = _baseline()

    save_baseline(path, baseline)
    loaded = load_baseline(path)

    assert loaded == baseline


def test_find_regressions_is_empty_when_nothing_dropped() -> None:
    previous = _baseline(assertions=0.9, scores={"helpfulness": 0.8})
    current = _baseline(assertions=0.95, scores={"helpfulness": 0.82})

    assert find_regressions(previous=previous, current=current) == []


def test_find_regressions_flags_an_assertions_drop() -> None:
    previous = _baseline(assertions=0.9, scores={})
    current = _baseline(assertions=0.85, scores={})

    regressions = find_regressions(previous=previous, current=current)

    assert len(regressions) == 1
    assert "assertions" in regressions[0]


def test_find_regressions_flags_a_score_drop_beyond_threshold() -> None:
    previous = _baseline(
        assertions=1.0,
        scores={"helpfulness": 0.8},
        thresholds=BaselineThresholds(max_score_drop=0.05),
    )
    current = _baseline(assertions=1.0, scores={"helpfulness": 0.7})

    regressions = find_regressions(previous=previous, current=current)

    assert len(regressions) == 1
    assert "helpfulness" in regressions[0]


def test_find_regressions_tolerates_a_score_drop_within_threshold() -> None:
    previous = _baseline(
        assertions=1.0,
        scores={"helpfulness": 0.8},
        thresholds=BaselineThresholds(max_score_drop=0.05),
    )
    current = _baseline(assertions=1.0, scores={"helpfulness": 0.76})

    assert find_regressions(previous=previous, current=current) == []


def test_find_regressions_flags_a_score_missing_from_the_new_run() -> None:
    previous = _baseline(assertions=1.0, scores={"helpfulness": 0.8})
    current = _baseline(assertions=1.0, scores={})

    regressions = find_regressions(previous=previous, current=current)

    assert len(regressions) == 1
    assert "helpfulness" in regressions[0]


def test_a_template_baseline_file_keeps_its_shape_without_decision_fields(
    tmp_path: Path,
) -> None:
    """Router and chitchat baselines gain no empty decision keys (#120)."""
    path = tmp_path / "sample.json"

    save_baseline(path, _baseline())

    text = path.read_text()
    assert "decision_thresholds" not in text
    assert '"cases"' not in text


def test_a_case_probability_spread_is_max_minus_min() -> None:
    assert CaseProbability(mean=0.8, min=0.7, max=0.9, runs=3).spread == pytest.approx(0.2)


def test_find_changes_names_version_model_and_threshold_differences() -> None:
    previous = _baseline(version=1, decision_thresholds={"pain": 0.5, "gone": 0.2})
    current = _baseline(
        version=2, model="typesafe/jev-1.14", decision_thresholds={"pain": 0.3, "new": 0.4}
    )

    assert find_changes(previous=previous, current=current) == [
        "version changed from 1 to 2",
        "model changed from 'openai/gpt-5-mini' to 'typesafe/jev-1.14'",
        "threshold 'gone' changed from 0.2 to none",
        "threshold 'new' changed from none to 0.4",
        "threshold 'pain' changed from 0.5 to 0.3",
    ]


def test_find_changes_is_empty_for_the_same_run() -> None:
    assert find_changes(previous=_baseline(), current=_baseline()) == []
