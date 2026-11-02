from pathlib import Path

import pytest

from scripts.check_suppressions import find_violations, main


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "module.py"
    path.write_text(source)
    return path


def test_no_suppression_without_a_reason_in_src_and_scripts() -> None:
    assert find_violations(Path("src")) == []
    assert find_violations(Path("scripts")) == []


def test_detects_a_bare_type_ignore(tmp_path: Path) -> None:
    path = _write(tmp_path, "x = 1\ny: int = 'a'  # type: ignore\n")

    violations = find_violations(tmp_path)

    assert [(v.path, v.line) for v in violations] == [(path, 2)]


def test_detects_a_coded_type_ignore_without_a_reason(tmp_path: Path) -> None:
    path = _write(tmp_path, "y: int = 'a'  # type: ignore[assignment]\n")

    violations = find_violations(tmp_path)

    assert [(v.path, v.line) for v in violations] == [(path, 1)]


def test_detects_a_bare_noqa(tmp_path: Path) -> None:
    path = _write(tmp_path, "import os\nimport sys  # noqa\n")

    violations = find_violations(tmp_path)

    assert [(v.path, v.line) for v in violations] == [(path, 2)]


def test_detects_a_coded_noqa_without_a_reason(tmp_path: Path) -> None:
    path = _write(tmp_path, "import sys  # noqa: F401\n")

    violations = find_violations(tmp_path)

    assert [(v.path, v.line) for v in violations] == [(path, 1)]


def test_detects_a_pragma_without_a_reason(tmp_path: Path) -> None:
    path = _write(tmp_path, "if True:\n    pass  # pragma: no cover\n")

    violations = find_violations(tmp_path)

    assert [(v.path, v.line) for v in violations] == [(path, 2)]


def test_accepts_a_suppression_with_a_code_and_a_reason(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a: int = 'a'  # type: ignore[assignment]  # the stub is wrong\n"
        "import sys  # noqa: F401 -- re-exported\n"
        "if True:\n"
        "    pass  # pragma: no cover -- only runs via direct execution\n",
    )

    assert find_violations(tmp_path) == []


def test_ignores_suppression_text_inside_a_string_literal(tmp_path: Path) -> None:
    _write(
        tmp_path,
        'a = "x  # type: ignore"\nb = \'# noqa\'\nc = """\n# pragma: no cover\n"""\n',
    )

    assert find_violations(tmp_path) == []


def test_main_returns_zero_when_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "x = 1\n")

    assert main([tmp_path]) == 0
    assert capsys.readouterr().out == ""


def test_main_prints_violations_and_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path, "import sys  # noqa\n")

    assert main([tmp_path]) == 1
    assert f"{path}:1" in capsys.readouterr().out


def test_ignores_a_comment_that_only_mentions_a_suppression(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "# keep this; no noqa needed here\n"
        "x = 1  # do not add a bare type: ignore or a pragma: no cover\n",
    )

    assert find_violations(tmp_path) == []


def test_accepts_space_separated_noqa_codes_and_flags_an_empty_type_ignore_code(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        "import sys  # noqa: F401 E402 -- re-exported\ny: int = 'a'  # type: ignore[ ]  # why\n",
    )

    violations = find_violations(tmp_path)

    assert [(v.path, v.line) for v in violations] == [(path, 2)]


def test_a_missing_root_is_an_error_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        find_violations(tmp_path / "missing")
