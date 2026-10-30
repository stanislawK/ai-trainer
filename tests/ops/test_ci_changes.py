"""`scripts/ci_changes.py` maps a pull request's changed paths to the CI jobs it needs (ticket #357,
ADR-0013 "Parallel, path-aware CI"): docs skip everything, `ops` and `e2e` run on their lists, and
a path no rule names runs every job."""

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
