"""Decision sets are evaluated like templates: labels graded in code at the `Settings`
thresholds, each case's probabilities recorded, thresholds kept in the baseline (ADR-0009,
ADR-0020, #120). A fake `DecisionModel` stands in for Jev (ADR-0013)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from pydantic_ai import models
from pydantic_ai.models.decision import (
    ChoiceAnswer,
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
    ScoreAnswer,
)
from pydantic_ai.usage import RequestUsage

from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.baseline import Baseline, CaseProbability, save_baseline
from ai_trainer.llm.evals.decisions import DecisionEvalRunResult, run_decision_eval
from ai_trainer.llm.evals.runner import CaseFailedError, EvaluatorFailedError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "decisions"
SNAPSHOT = "typesafe/jev-1.13-20260917"


class ScriptedJev(DecisionModel[None]):
    """Answers the sample set, taking `mentions_pain` (and optionally `effort`) from a script so
    repeats can differ. By default `effort` is a confident 2."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(
        self,
        pain: Iterator[float],
        *,
        effort: Iterator[ScoreAnswer] | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__()
        self._pain = pain
        self._effort = effort
        self._error = error

    @property
    def model_name(self) -> str:
        return SNAPSHOT

    @property
    def system(self) -> str:
        return "fake"

    @property
    def base_url(self) -> str:
        return "https://jev.invalid"

    async def decide(
        self, request: DecisionRequest, model_settings: DecisionModelSettings
    ) -> DecisionResponse:
        if self._error is not None:
            raise self._error
        answers: dict[str, DecisionAnswer] = {
            "mentions_pain": NoulAnswer(noul=next(self._pain)),
            "sport": ChoiceAnswer(
                choice="climbing",
                confidence=0.75,
                probabilities={"climbing": 0.84, "gym": 0.16, "cycling": 0.0},
            ),
            "effort": next(self._effort)
            if self._effort is not None
            else ScoreAnswer(score=1.99, confidence=0.99, probabilities={0: 0.0, 1: 0.01, 2: 0.99}),
        }
        return DecisionResponse(
            answers=answers, model_name=SNAPSHOT, usage=RequestUsage(input_tokens=350)
        )


@pytest.fixture(autouse=True)
def _allow_fake_decision_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DecisionModel.request` checks the no-model-requests guard even for a fake subclass."""
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _write_dataset(root: Path, cases: list[dict[str, object]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sample.yaml").write_text(yaml.safe_dump({"name": "sample", "cases": cases}))


_PAIN_CASE: dict[str, object] = {
    "name": "shoulder twinge",
    "inputs": {"prompt": "Bouldered two hours, left shoulder twinged."},
    "expected_output": {"mentions_pain": True, "sport": "climbing", "effort": 2},
}
_NO_PAIN_CASE: dict[str, object] = {
    "name": "easy spin",
    "inputs": {"prompt": "Easy spin, felt great."},
    "expected_output": {"mentions_pain": False},
}


async def _run(
    tmp_path: Path,
    model: DecisionModel[None],
    *,
    thresholds: dict[str, float] | None = None,
    repeat: int = 1,
) -> DecisionEvalRunResult:
    return await run_decision_eval(
        loader=DecisionSetLoader(root=FIXTURES),
        decision_id="sample",
        version=1,
        model=model,
        model_id="typesafe/jev-1.13",
        thresholds={"mentions_pain": 0.3} if thresholds is None else thresholds,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        repeat=repeat,
    )


async def test_labels_are_graded_in_code_at_the_settings_threshold(tmp_path: Path) -> None:
    """P(pain) 0.35 is pain at a 0.3 threshold, though `bool`'s 0.5 cut would say no."""
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE, _NO_PAIN_CASE])

    result = await run_decision_eval(
        loader=DecisionSetLoader(root=FIXTURES),
        decision_id="sample",
        version=1,
        model=ScriptedJev(iter([0.35, 0.35])),
        model_id="typesafe/jev-1.13",
        thresholds={"mentions_pain": 0.3},
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    by_name = {case.name: case for case in result.report.cases}
    pain = by_name["shoulder twinge"].assertions
    assert {name: bool(a.value) for name, a in pain.items()} == {
        "mentions_pain": True,
        "sport": True,
        "effort": True,
    }
    no_pain = by_name["easy spin"].assertions
    assert {name: bool(a.value) for name, a in no_pain.items()} == {"mentions_pain": False}


async def test_the_baseline_stores_version_model_thresholds_pass_rate_and_probabilities(
    tmp_path: Path,
) -> None:
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])

    await _run(tmp_path, ScriptedJev(iter([0.82])))

    saved = Baseline.model_validate_json((tmp_path / "baselines" / "sample.json").read_text())
    assert (saved.template_id, saved.version, saved.model) == ("sample", 1, "typesafe/jev-1.13")
    assert saved.assertions == 1.0
    assert saved.decision_thresholds == {"mentions_pain": 0.3}
    assert saved.cases is not None
    probabilities = saved.cases["shoulder twinge"]
    assert probabilities["mentions_pain"] == CaseProbability(mean=0.82, min=0.82, max=0.82, runs=1)
    # A choice records the probability of the option it picked; a score, its expected level.
    assert probabilities["sport"] == CaseProbability(mean=0.84, min=0.84, max=0.84, runs=1)
    assert probabilities["effort"] == CaseProbability(mean=1.99, min=1.99, max=1.99, runs=1)
    report = (tmp_path / "reports" / "sample.json").read_text()
    assert '"probabilities"' in report


async def test_repeat_reports_the_probability_spread_per_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])

    result = await _run(tmp_path, ScriptedJev(iter([0.7, 0.9, 0.8])), repeat=3)

    assert result.baseline.cases is not None
    stats = result.baseline.cases["shoulder twinge"]["mentions_pain"]
    assert (stats.min, stats.max) == (0.7, 0.9)
    assert stats.mean == pytest.approx(0.8)
    assert stats.spread == pytest.approx(0.2)
    assert stats.runs == 3
    assert not stats.flips
    out = capsys.readouterr().out
    assert "probability spread" in out
    spread_line = next(
        line for line in out.splitlines() if "mentions_pain" in line and "spread" in line
    )
    assert "shoulder twinge" in spread_line
    assert "0.20" in spread_line
    assert "3 runs" in spread_line


async def test_a_score_records_its_expected_level_not_the_rounded_levels_probability(
    tmp_path: Path,
) -> None:
    """Jev rounds the expected level: 1.0 from a 0-or-2 split is level 1, which it gave 0%.
    The expected level is what jitters, so it is what the spread tracks (#120 skeptic)."""
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])
    split = ScoreAnswer(score=1.0, confidence=0.1, probabilities={0: 0.5, 1: 0.0, 2: 0.5})
    nudged = ScoreAnswer(score=1.4, confidence=0.1, probabilities={0: 0.3, 1: 0.0, 2: 0.7})

    result = await _run(
        tmp_path, ScriptedJev(iter([0.82, 0.82]), effort=iter([split, nudged])), repeat=2
    )

    assert result.baseline.cases is not None
    stats = result.baseline.cases["shoulder twinge"]["effort"]
    assert (stats.min, stats.max) == (1.0, 1.4)


async def test_a_case_whose_grade_differs_between_repeats_is_flagged_as_flipping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P(pain) either side of the threshold on two calls: the spread alone may look small."""
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])

    result = await _run(tmp_path, ScriptedJev(iter([0.29, 0.31])), repeat=2)

    assert result.baseline.cases is not None
    assert result.baseline.cases["shoulder twinge"]["mentions_pain"].flips
    assert not result.baseline.cases["shoulder twinge"]["sport"].flips
    flip_line = next(
        line for line in capsys.readouterr().out.splitlines() if line.endswith("  FLIPS")
    )
    assert "shoulder twinge / mentions_pain" in flip_line


async def test_a_threshold_change_is_reported_like_a_model_change(tmp_path: Path) -> None:
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])
    save_baseline(
        tmp_path / "baselines" / "sample.json",
        Baseline(
            template_id="sample",
            version=1,
            model="typesafe/jev-1.12",
            assertions=1.0,
            scores={},
            decision_thresholds={"mentions_pain": 0.5},
        ),
    )

    result = await _run(tmp_path, ScriptedJev(iter([0.82])))

    assert result.changes == [
        "model changed from 'typesafe/jev-1.12' to 'typesafe/jev-1.13'",
        "threshold 'mentions_pain' changed from 0.5 to 0.3",
    ]
    assert result.regressions == []


async def test_an_unchanged_rerun_reports_no_changes(tmp_path: Path) -> None:
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])
    await _run(tmp_path, ScriptedJev(iter([0.82])))

    result = await _run(tmp_path, ScriptedJev(iter([0.82])))

    assert result.changes == []


async def test_a_pass_rate_drop_is_a_regression(tmp_path: Path) -> None:
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])
    await _run(tmp_path, ScriptedJev(iter([0.82])))

    result = await _run(tmp_path, ScriptedJev(iter([0.1])))

    assert result.regressions
    assert "assertions" in result.regressions[0]


async def test_a_probability_label_without_a_threshold_fails_loudly(tmp_path: Path) -> None:
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])

    with pytest.raises(EvaluatorFailedError, match="mentions_pain"):
        await _run(tmp_path, ScriptedJev(iter([0.82])), thresholds={})

    assert not (tmp_path / "baselines" / "sample.json").exists()


async def test_a_label_for_a_question_the_set_lacks_fails_loudly(tmp_path: Path) -> None:
    _write_dataset(
        tmp_path / "datasets",
        [{**_PAIN_CASE, "expected_output": {"mentions_pain": True, "mood": "happy"}}],
    )

    with pytest.raises(EvaluatorFailedError, match="mood"):
        await _run(tmp_path, ScriptedJev(iter([0.82])))


async def test_a_failed_call_raises_instead_of_writing_a_baseline(tmp_path: Path) -> None:
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])

    with pytest.raises(CaseFailedError):
        await _run(tmp_path, ScriptedJev(iter([]), error=RuntimeError("401 from OpenRouter")))

    assert not (tmp_path / "baselines" / "sample.json").exists()


async def test_the_run_prints_the_thresholds_it_grades_at(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The log alone shows which threshold a run used, whatever `.env` set (#120 skeptic)."""
    _write_dataset(tmp_path / "datasets", [_PAIN_CASE])

    await _run(tmp_path, ScriptedJev(iter([0.82])), thresholds={"mentions_pain": 0.25})

    assert "graded at thresholds: mentions_pain >= 0.25" in capsys.readouterr().out


async def test_an_unlabelled_score_whose_level_changes_between_repeats_flips(
    tmp_path: Path,
) -> None:
    """No label means no grade, but a level that rounds differently is still unstable."""
    _write_dataset(tmp_path / "datasets", [{**_PAIN_CASE, "expected_output": {}}])
    low = ScoreAnswer(score=1.45, confidence=0.5, probabilities={0: 0.1, 1: 0.35, 2: 0.55})
    high = ScoreAnswer(score=1.55, confidence=0.5, probabilities={0: 0.05, 1: 0.35, 2: 0.6})

    result = await _run(
        tmp_path, ScriptedJev(iter([0.82, 0.82]), effort=iter([low, high])), repeat=2
    )

    assert result.baseline.cases is not None
    assert result.baseline.cases["shoulder twinge"]["effort"].flips
    assert not result.baseline.cases["shoulder twinge"]["mentions_pain"].flips
