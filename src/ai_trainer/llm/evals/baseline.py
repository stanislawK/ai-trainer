from pathlib import Path

from pydantic import BaseModel


class BaselineThresholds(BaseModel):
    """Regression tolerances (ADR-0009): by default no drop in the code-graded pass rate
    (`assertions`), and at most a 0.05 drop in any average score. Thresholds live in the
    baseline file, so a human can tune them (e.g. through `/tune-prompt`)."""

    max_assertions_drop: float = 0.0
    max_score_drop: float = 0.05


class Baseline(BaseModel):
    """A committed `evals/baselines/<template_id>.json` (ADR-0009)."""

    template_id: str
    version: int
    model: str
    assertions: float
    scores: dict[str, float]
    thresholds: BaselineThresholds = BaselineThresholds()


def load_baseline(path: Path) -> Baseline | None:
    if not path.exists():
        return None
    return Baseline.model_validate_json(path.read_text())


def save_baseline(path: Path, baseline: Baseline) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.model_dump_json(indent=2) + "\n")


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
