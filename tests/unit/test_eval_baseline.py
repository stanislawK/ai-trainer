from pathlib import Path

from ai_trainer.llm.evals.baseline import (
    Baseline,
    BaselineThresholds,
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
