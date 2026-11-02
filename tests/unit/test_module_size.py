from collections.abc import Mapping
from pathlib import Path

import ai_trainer

SRC_DIR = Path(ai_trainer.__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
MAX_MODULE_LINES = 400

# Over-limit modules, relative to src/, each with why it is allowed (ADR-0003, Size rule).
EXEMPTIONS: dict[str, str] = {
    "ai_trainer/main.py": "composition root, ADR-0003",
    "ai_trainer/llm/evals/cli.py": "no split ticket yet",
    "ai_trainer/adapters/session_draft_lifecycle.py": "no split ticket yet",
    "ai_trainer/adapters/exercise_repository.py": "no split ticket yet",
}


def module_size_problems(root: Path, limit: int, exemptions: Mapping[str, str]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for module in sorted(root.rglob("*.py")):
        path = module.relative_to(root).as_posix()
        seen.add(path)
        lines = len(module.read_text(encoding="utf-8").splitlines())
        if path in exemptions:
            if lines <= limit:
                problems.append(
                    f"{path} has {lines} lines, within the limit of {limit}: "
                    "stale exemption, remove it"
                )
        elif lines > limit:
            problems.append(f"{path} has {lines} lines, over the limit of {limit}")
    for path, reason in exemptions.items():
        if not reason.strip():
            problems.append(f"{path} is exempt with an empty reason")
        if path not in seen:
            problems.append(f"{path} is exempt but does not exist: stale exemption")
    return problems


def _write_module(root: Path, name: str, lines: int) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n" * lines)


def test_src_tree_is_within_the_size_rule() -> None:
    assert module_size_problems(SRC_DIR, MAX_MODULE_LINES, EXEMPTIONS) == []


def test_exemptions_are_exactly_the_four_known_modules() -> None:
    assert set(EXEMPTIONS) == {
        "ai_trainer/main.py",
        "ai_trainer/llm/evals/cli.py",
        "ai_trainer/adapters/session_draft_lifecycle.py",
        "ai_trainer/adapters/exercise_repository.py",
    }
    assert EXEMPTIONS["ai_trainer/main.py"] == "composition root, ADR-0003"


def test_module_over_the_limit_fails_with_path_and_line_count(tmp_path: Path) -> None:
    _write_module(tmp_path, "pkg/big.py", 401)

    problems = module_size_problems(tmp_path, MAX_MODULE_LINES, {})

    assert len(problems) == 1
    assert "pkg/big.py" in problems[0]
    assert "401 lines" in problems[0]


def test_module_at_the_limit_passes(tmp_path: Path) -> None:
    _write_module(tmp_path, "pkg/edge.py", 400)

    assert module_size_problems(tmp_path, MAX_MODULE_LINES, {}) == []


def test_exempted_module_over_the_limit_passes(tmp_path: Path) -> None:
    _write_module(tmp_path, "pkg/big.py", 401)

    assert module_size_problems(tmp_path, MAX_MODULE_LINES, {"pkg/big.py": "why"}) == []


def test_exempted_module_under_the_limit_is_a_stale_exemption(tmp_path: Path) -> None:
    _write_module(tmp_path, "pkg/small.py", 399)

    problems = module_size_problems(tmp_path, MAX_MODULE_LINES, {"pkg/small.py": "split in #1"})

    assert len(problems) == 1
    assert "pkg/small.py" in problems[0]
    assert "stale exemption" in problems[0]


def test_exemption_for_a_missing_module_is_stale(tmp_path: Path) -> None:
    problems = module_size_problems(tmp_path, MAX_MODULE_LINES, {"gone.py": "why"})

    assert len(problems) == 1
    assert "gone.py" in problems[0]
    assert "stale exemption" in problems[0]


def test_exemption_with_empty_reason_fails(tmp_path: Path) -> None:
    _write_module(tmp_path, "pkg/big.py", 401)

    for reason in ("", "   "):
        problems = module_size_problems(tmp_path, MAX_MODULE_LINES, {"pkg/big.py": reason})

        assert len(problems) == 1
        assert "pkg/big.py" in problems[0]
        assert "empty reason" in problems[0]


def test_orm_tables_defines_no_size_limit() -> None:
    assert "MAX_MODULE_LINES" not in (TESTS_DIR / "test_orm_tables.py").read_text()
