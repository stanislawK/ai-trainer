import ast
from collections.abc import Mapping
from pathlib import Path

import ai_trainer

SRC_DIR = Path(ai_trainer.__file__).resolve().parent.parent
ENVIRONMENT_NAMES = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"})

# Files under src/ allowed to read the environment, relative to src/, each with why (ADR-0002,
# invariant 1). Only `Settings` reads configuration, so this stays empty.
ALLOWED: dict[str, str] = {}


def _os_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(a.asname or a.name for a in node.names if a.name == "os")
    return aliases


def _reads_in(tree: ast.AST) -> list[int]:
    aliases = _os_aliases(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ENVIRONMENT_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module == "os" and node.level == 0:
            lines.extend(node.lineno for a in node.names if a.name in ENVIRONMENT_NAMES)
    return sorted(lines)


def environment_read_problems(root: Path, allowed: Mapping[str, str]) -> list[str]:
    problems: list[str] = []
    hit_files: set[str] = set()
    seen: set[str] = set()
    for module in sorted(root.rglob("*.py")):
        path = module.relative_to(root).as_posix()
        seen.add(path)
        lines = _reads_in(ast.parse(module.read_text(encoding="utf-8"), filename=str(module)))
        if not lines:
            continue
        hit_files.add(path)
        if path not in allowed:
            problems.extend(
                f"{module}:{line} reads the environment outside Settings" for line in lines
            )
    for path, reason in allowed.items():
        if not reason.strip():
            problems.append(f"{path} is allowed with an empty reason")
        if path not in seen:
            problems.append(f"{path} is allowed but does not exist: stale entry")
        elif path not in hit_files:
            problems.append(f"{path} is allowed but reads no environment: stale entry")
    return problems


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "module.py"
    path.write_text(source)
    return path


def test_src_reads_the_environment_nowhere() -> None:
    assert ALLOWED == {}
    assert environment_read_problems(SRC_DIR, ALLOWED) == []


def test_detects_environ_subscript(tmp_path: Path) -> None:
    path = _write(tmp_path, 'import os\n\nx = os.environ["X"]\n')

    assert environment_read_problems(tmp_path, {}) == [
        f"{path}:3 reads the environment outside Settings"
    ]


def test_detects_environ_get(tmp_path: Path) -> None:
    path = _write(tmp_path, 'import os\nx = os.environ.get("X")\n')

    assert [p.split(" ")[0] for p in environment_read_problems(tmp_path, {})] == [f"{path}:2"]


def test_detects_getenv(tmp_path: Path) -> None:
    path = _write(tmp_path, 'import os\nx = os.getenv("X")\n')

    assert [p.split(" ")[0] for p in environment_read_problems(tmp_path, {})] == [f"{path}:2"]


def test_detects_putenv(tmp_path: Path) -> None:
    path = _write(tmp_path, 'import os\nos.putenv("X", "1")\n')

    assert [p.split(" ")[0] for p in environment_read_problems(tmp_path, {})] == [f"{path}:2"]


def test_detects_from_os_import_environ(tmp_path: Path) -> None:
    path = _write(tmp_path, "import sys\nfrom os import environ\n")

    assert [p.split(" ")[0] for p in environment_read_problems(tmp_path, {})] == [f"{path}:2"]


def test_detects_from_os_import_getenv_among_other_names(tmp_path: Path) -> None:
    path = _write(tmp_path, "from os import getenv, path\n")

    assert [p.split(" ")[0] for p in environment_read_problems(tmp_path, {})] == [f"{path}:1"]


def test_detects_an_aliased_os_import(tmp_path: Path) -> None:
    path = _write(tmp_path, 'import os as operating_system\nx = operating_system.getenv("X")\n')

    assert [p.split(" ")[0] for p in environment_read_problems(tmp_path, {})] == [f"{path}:2"]


def test_ignores_os_path_join(tmp_path: Path) -> None:
    _write(tmp_path, 'import os\nfrom os import path\nx = os.path.join("a", "b")\n')

    assert environment_read_problems(tmp_path, {}) == []


def test_ignores_a_string_or_comment_mentioning_os_environ(tmp_path: Path) -> None:
    _write(
        tmp_path,
        'a = "os.environ[\\"X\\"]"\n'
        "# os.getenv is forbidden here\n"
        'b = """\nfrom os import environ\n"""\n',
    )

    assert environment_read_problems(tmp_path, {}) == []


def test_ignores_an_environ_attribute_on_another_object(tmp_path: Path) -> None:
    _write(tmp_path, "import os\nx = config.environ\ny = settings.getenv\n")

    assert environment_read_problems(tmp_path, {}) == []


def test_allowed_file_passes(tmp_path: Path) -> None:
    _write(tmp_path, 'import os\nx = os.getenv("X")\n')

    assert environment_read_problems(tmp_path, {"module.py": "bootstraps the settings"}) == []


def test_allowed_entry_with_empty_reason_fails(tmp_path: Path) -> None:
    _write(tmp_path, 'import os\nx = os.getenv("X")\n')

    for reason in ("", "   "):
        problems = environment_read_problems(tmp_path, {"module.py": reason})

        assert len(problems) == 1
        assert "module.py" in problems[0]
        assert "empty reason" in problems[0]


def test_allowed_entry_for_a_file_that_reads_nothing_is_stale(tmp_path: Path) -> None:
    _write(tmp_path, "x = 1\n")

    problems = environment_read_problems(tmp_path, {"module.py": "why"})

    assert len(problems) == 1
    assert "stale entry" in problems[0]


def test_allowed_entry_for_a_missing_file_is_stale(tmp_path: Path) -> None:
    problems = environment_read_problems(tmp_path, {"gone.py": "why"})

    assert len(problems) == 1
    assert "gone.py" in problems[0]
    assert "stale entry" in problems[0]
