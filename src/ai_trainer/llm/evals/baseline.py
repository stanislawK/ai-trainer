from pathlib import Path

from pydantic import BaseModel, computed_field


class BaselineThresholds(BaseModel):
    """Regression tolerances (ADR-0009): by default no drop in the code-graded pass rate
    (`assertions`), and at most a 0.05 drop in any average score. Thresholds live in the
    baseline file, so a human can tune them (e.g. through `/tune-prompt`)."""

    max_assertions_drop: float = 0.0
    max_score_drop: float = 0.05


class CaseProbability(BaseModel):
    """One question's value on one case over the repeats that ran: P(yes), a choice's P(pick)
    or a score's expected level. Jev jitters between calls (ADR-0020), so the spread shows how
    far a value near its threshold can move, and `flips` that its grade did change."""

    mean: float
    min: float
    max: float
    runs: int
    flips: bool = False

    @computed_field  # type: ignore[prop-decorator]  # pydantic's documented property form
    @property
    def spread(self) -> float:
        return self.max - self.min


class Baseline(BaseModel):
    """A committed `evals/baselines/<template_id>.json` (ADR-0009).

    A decision set's baseline also holds the thresholds its labels were graded at and each
    case's probabilities (ADR-0020); both stay `None`, and out of the file, for a template."""

    template_id: str
    version: int
    model: str
    assertions: float
    scores: dict[str, float]
    thresholds: BaselineThresholds = BaselineThresholds()
    decision_thresholds: dict[str, float] | None = None
    cases: dict[str, dict[str, CaseProbability]] | None = None


def load_baseline(path: Path) -> Baseline | None:
    if not path.exists():
        return None
    return Baseline.model_validate_json(path.read_text())


def save_baseline(path: Path, baseline: Baseline) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.model_dump_json(indent=2, exclude_none=True) + "\n")


def find_regressions(*, previous: Baseline, current: Baseline) -> list[str]:
    """Compares `current` against `previous` using `previous`'s thresholds — the committed
    file governs what the next run is judged against (ADR-0009)."""
    thresholds = previous.thresholds
    regressions: list[str] = []

    min_assertions = previous.assertions - thresholds.max_assertions_drop
    if current.assertions < min_assertions:
        regressions.append(
            f"assertions dropped from {previous.assertions:.3f} to {current.assertions:.3f} "
            f"(min {min_assertions:.3f})"
        )

    for name, previous_score in previous.scores.items():
        min_score = previous_score - thresholds.max_score_drop
        current_score = current.scores.get(name)
        if current_score is None:
            regressions.append(f"score {name!r} is missing from the new run")
        elif current_score < min_score:
            regressions.append(
                f"score {name!r} dropped from {previous_score:.3f} to {current_score:.3f} "
                f"(min {min_score:.3f})"
            )

    return regressions


def find_changes(*, previous: Baseline, current: Baseline) -> list[str]:
    """What the run changed besides its results: the version, the model, or a decision
    threshold. A threshold change is reviewed like a model change (ADR-0009 amendment)."""
    changes: list[str] = []
    if current.version != previous.version:
        changes.append(f"version changed from {previous.version} to {current.version}")
    if current.model != previous.model:
        changes.append(f"model changed from {previous.model!r} to {current.model!r}")
    before = previous.decision_thresholds or {}
    after = current.decision_thresholds or {}
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) != after.get(name):
            changes.append(
                f"threshold {name!r} changed from {_or_none(before.get(name))} "
                f"to {_or_none(after.get(name))}"
            )
    return changes


def _or_none(value: float | None) -> str:
    return "none" if value is None else f"{value:g}"
