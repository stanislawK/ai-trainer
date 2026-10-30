"""`scripts/ci_changes.py` maps a pull request's changed paths to the CI jobs it needs (ticket #357,
ADR-0013 "Parallel, path-aware CI"): docs skip everything, `ops` and `e2e` run on their lists, and
a path no rule names runs every job."""

import re
import subprocess
from pathlib import Path

import pytest

from scripts import ci_changes as cc

ALL = {"static": True, "test": True, "ops": True, "e2e": True}
NONE = {"static": False, "test": False, "ops": False, "e2e": False}
STATIC_TEST = {"static": True, "test": True, "ops": False, "e2e": False}


def test_docs_only_skips_every_job() -> None:
    paths = [
        "docs/adr/0013-testing-and-quality.md",
        "docs/prd/README.md",
        "README.md",
        "CLAUDE.md",
        ".claude/rules/tests.md",
        ".claude/skills/apply-ticket/SKILL.md",
    ]
    assert cc.flags(paths) == NONE


def test_prompt_template_runs_static_test_and_e2e() -> None:
    flags = cc.flags(["src/ai_trainer/llm/prompts/chitchat/system.md"])
    assert flags == {"static": True, "test": True, "ops": False, "e2e": True}


def test_deploy_runs_ops() -> None:
    flags = cc.flags(["deploy/deploy.sh"])
    assert flags == {"static": True, "test": True, "ops": True, "e2e": False}


def test_claude_hooks_run_test() -> None:
    assert cc.flags([".claude/hooks/guard_release.py"]) == STATIC_TEST


@pytest.mark.parametrize("path", ["alembic.ini", ".python-version", ".github/CODEOWNERS", "x.txt"])
def test_unknown_path_runs_every_job(path: str) -> None:
    assert cc.flags([path]) == ALL


def test_unknown_path_among_docs_still_runs_every_job() -> None:
    assert cc.flags(["docs/adr/README.md", "alembic.ini"]) == ALL


def test_src_only_skips_only_ops() -> None:
    flags = cc.flags(["src/ai_trainer/web/app.py", "src/ai_trainer/domain/clock.py"])
    assert flags == {"static": True, "test": True, "ops": False, "e2e": True}


def test_openapi_snapshot_alone_runs_static() -> None:
    assert cc.flags(["docs/api/openapi.json"]) == STATIC_TEST


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/test_clock.py",
        "tests/integration/conftest.py",
        "tests/migrations/test_x.py",
        "tests/fixtures/x.json",
        "evals/field/chat.json",
        "migrations/versions/abc_x.py",
    ],
)
def test_known_static_and_test_paths_skip_ops(path: str) -> None:
    flags = cc.flags([path])
    assert flags["static"]
    assert flags["test"]
    assert not flags["ops"]


@pytest.mark.parametrize(
    "path",
    [
        "deploy/alloy/config.alloy",
        "scripts/worktree.py",
        "compose.yaml",
        "compose.prod.yaml",
        "Dockerfile",
        "Makefile",
        "tests/ops/test_deploy_script.py",
        "tests/conftest.py",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/ci.yml",
    ],
)
def test_ops_list_runs_ops(path: str) -> None:
    flags = cc.flags([path])
    assert flags["ops"]
    assert flags["static"]
    assert flags["test"]


@pytest.mark.parametrize(
    "path",
    [
        "src/ai_trainer/app.py",
        "migrations/versions/abc_x.py",
        "tests/e2e/test_theme.py",
        "tests/conftest.py",
        "Dockerfile",
        "compose.yaml",
        ".env.example",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/release.yml",
    ],
)
def test_e2e_list_runs_e2e(path: str) -> None:
    assert cc.flags([path])["e2e"]


@pytest.mark.parametrize("path", ["compose.prod.yaml", "deploy/deploy.sh", "scripts/x.py"])
def test_ops_only_paths_skip_e2e(path: str) -> None:
    assert not cc.flags([path])["e2e"]


def test_docs_outside_the_skip_list_are_not_skipped() -> None:
    """Only `docs/**` is skippable, not any `*.md`: a nested README under `src/` runs."""
    assert cc.flags(["src/ai_trainer/README.md"])["test"]
    assert cc.flags([".claude/agents/plan-critic.md"]) == ALL


def test_empty_diff_runs_every_job() -> None:
    assert cc.flags([]) == ALL


def test_output_lines_are_github_outputs() -> None:
    assert cc.output_lines(STATIC_TEST) == ["static=true", "test=true", "ops=false", "e2e=false"]


def test_all_flag_runs_every_job(capsys: pytest.CaptureFixture[str]) -> None:
    """A push to `main` passes `--all`: every job runs whatever changed."""
    assert cc.main(["--all"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "static=true",
        "test=true",
        "ops=true",
        "e2e=true",
    ]


def test_base_or_all_is_required() -> None:
    with pytest.raises(SystemExit):
        cc.main([])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "ci@example.com")
    _git(tmp_path, "config", "user.name", "ci")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("x = 1\n" * 20)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/a.md").write_text("a\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def test_main_prints_github_outputs_from_a_real_diff(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "docs/a.md").write_text("b\n")
    _git(repo, "commit", "-q", "-am", "docs")

    assert cc.main(["--base", "main", "--root", str(repo)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "static=false",
        "test=false",
        "ops=false",
        "e2e=false",
    ]


def test_diff_is_against_the_merge_base(repo: Path) -> None:
    """A commit on the base after the branch left it never counts as this branch's change."""
    (repo / "docs/a.md").write_text("b\n")
    _git(repo, "commit", "-q", "-am", "docs")
    _git(repo, "checkout", "-q", "main")
    (repo / "deploy").mkdir()
    (repo / "deploy/x.sh").write_text("true\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "on main")
    _git(repo, "checkout", "-q", "feature")

    assert cc.changed_paths("main", repo) == ["docs/a.md"]


def test_rename_out_of_src_into_docs_still_runs(repo: Path) -> None:
    """`--no-renames` lists both sides, so moving code into `docs/` can't hide the `src/` path."""
    _git(repo, "mv", "src/app.py", "docs/app.py")
    _git(repo, "commit", "-q", "-m", "move")

    paths = cc.changed_paths("main", repo)

    assert sorted(paths) == ["docs/app.py", "src/app.py"]
    assert cc.flags(paths)["e2e"]


@pytest.mark.parametrize(
    "path",
    [
        # tests/ops imports these and reads cli.py and .env.example (#357 code review).
        "src/ai_trainer/settings.py",
        "src/ai_trainer/adapters/metrics.py",
        "src/ai_trainer/adapters/log_records.py",
        "src/ai_trainer/domain/llm_calls.py",
        "src/ai_trainer/cli.py",
        ".env.example",
    ],
)
def test_src_files_the_ops_tests_read_run_ops(path: str) -> None:
    assert cc.flags([path])["ops"]


@pytest.mark.parametrize(
    "path", ["scripts/build_css.sh", "scripts/css_versions.sh", "scripts/css_tools.sh"]
)
def test_css_scripts_the_image_builds_with_run_e2e(path: str) -> None:
    assert cc.flags([path])["e2e"]


ROOT = Path(__file__).resolve().parents[2]
_IMPORT = re.compile(r"^\s*from ai_trainer((?:\.\w+)+) import", re.MULTILINE)
_ROOT_READ = re.compile(r"ROOT / \"([^\"]+)\"")
_SRC_LITERAL = re.compile(r"\"(src/[^\"]+)\"")


def _covered(path: str, patterns: tuple[str, ...]) -> bool:
    """A file, or a directory any of whose files the patterns match."""
    return cc._matches(path, patterns) or cc._matches(f"{path}/x", patterns)


def _ops_test_dependencies() -> set[str]:
    found: set[str] = set()
    for module in (ROOT / "tests/ops").glob("*.py"):
        if module.name == "test_ci_changes.py":  # names paths as test data, not as reads
            continue
        text = module.read_text()
        for dotted in _IMPORT.findall(text):
            base = "src/ai_trainer" + dotted.replace(".", "/")
            found.add(f"{base}.py" if (ROOT / f"{base}.py").exists() else base)
        found.update(_ROOT_READ.findall(text))
        found.update(_SRC_LITERAL.findall(text))
    return found


def test_every_file_the_ops_tests_read_or_import_runs_ops() -> None:
    """A new `from ai_trainer…` import or `ROOT / "…"` read in tests/ops fails here until `_OPS`
    names it, so a pull request touching it can't skip the tests that check it."""
    dependencies = _ops_test_dependencies()
    assert "src/ai_trainer/cli.py" in dependencies
    assert [path for path in sorted(dependencies) if not _covered(path, cc._OPS)] == []


def _dockerfile_copy_sources() -> set[str]:
    sources: set[str] = set()
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        words = line.split()
        if not words or words[0] != "COPY" or any(w.startswith("--from") for w in words):
            continue
        sources.update(w for w in words[1:-1] if not w.startswith("--") and w != ".")
    return sources


def test_every_file_the_dockerfile_copies_runs_e2e() -> None:
    """The image the e2e job builds starts from these files, so changing one runs `e2e`."""
    sources = _dockerfile_copy_sources()
    assert "scripts/build_css.sh" in sources
    assert [path for path in sorted(sources) if not _covered(path, cc._E2E)] == []
