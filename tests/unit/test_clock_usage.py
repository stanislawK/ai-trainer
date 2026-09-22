import re
from pathlib import Path

_FORBIDDEN = re.compile(r"datetime\.now\(|date\.today\(|\.utcnow\(")
_ALLOWED_FILE = Path("src/ai_trainer/adapters/clock.py")


def test_no_wall_clock_read_outside_the_clock_adapter() -> None:
    """ADR-0014 invariant 1: no `datetime.now()`, `date.today()` or `utcnow()` outside
    the `Clock` adapter."""
    violations = [
        f"{path}:{lineno}"
        for path in Path("src").rglob("*.py")
        if path != _ALLOWED_FILE
        for lineno, line in enumerate(path.read_text().splitlines(), start=1)
        if _FORBIDDEN.search(line)
    ]

    assert violations == []
