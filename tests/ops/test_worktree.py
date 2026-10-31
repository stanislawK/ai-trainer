"""`make worktree issue=<n>` / `make worktree-rm issue=<n>` (ADR-0035 parallel lanes, ticket #351):
one ticket gets its own checkout at `../ai-trainer-<n>` and its own database `ai_trainer_wt<n>`.

The script runs against a recording stand-in for git, gh, uv and code, and an in-memory set of
databases, so no checkout, network or Postgres is touched."""

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import worktree as wt

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "postgresql+psycopg://ai_trainer:ai_trainer@localhost:5432/ai_trainer"
WT_URL = "postgresql+psycopg://ai_trainer:ai_trainer@localhost:5432/ai_trainer_wt351"
TITLE = "infra: start a ticket in its own worktree and database"
BRANCH = "infra/351-start-ticket-in-its-own"
ENV_TEXT = f"OPENROUTER_API_KEY=sk-test\nDATABASE_URL={BASE_URL}\nLOG_LEVEL=INFO\n"


class FakeRun:
    """Records every command; `fail` maps a command prefix to the error output it fails with."""

    def __init__(
        self, *, labels: Sequence[str] = ("area:infra", "status:todo"), remote_branch: bool = False
    ) -> None:
        self.calls: list[tuple[list[str], Path | None, dict[str, str]]] = []
        self.issue = {"title": TITLE, "labels": [{"name": name} for name in labels]}
        self.remote_branch = remote_branch
        self.local_branches: set[str] = set()
        self.fail: dict[tuple[str, ...], str] = {}
        self.worktrees = ""
        self.make_dir = True
        self.prs: list[dict[str, Any]] = []
        self.app_container: str | None = None
        self.toplevel = ""
        self.tips: dict[str, str] = {}
        self.dirty: set[Path] = set()

    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        command = list(args)
        self.calls.append((command, cwd, dict(env or {})))
        if command[:3] == ["git", "worktree", "add"]:
            # Real git makes the branch first, so it is there even when the add then fails.
            self.local_branches.add(command[4])
        for prefix, error in self.fail.items():
            if tuple(command[: len(prefix)]) == prefix:
                raise wt.CommandError(command, error)
        if command[:3] == ["gh", "issue", "view"]:
            return json.dumps(self.issue)
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps(self.prs)
        if command[:3] == ["git", "status", "--porcelain"]:
            return " M src/app.py\n" if cwd in self.dirty else ""
        if command[:2] == ["git", "rev-parse"] and command[2].startswith("refs/heads/"):
            return self.tips.get(command[2].removeprefix("refs/heads/"), "") + "\n"
        if command[:2] == ["docker", "inspect"]:
            if self.app_container is None:
                raise wt.CommandError(command, "Error: No such object: ai-trainer-app-1")
            return self.app_container
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return self.toplevel
        if command[:2] == ["git", "ls-remote"]:
            return f"abc123\trefs/heads/{command[-1]}\n" if self.remote_branch else ""
        local = command[-1].removeprefix("refs/heads/") in self.local_branches
        if command[:3] == ["git", "rev-parse", "--verify"] and not local:
            raise wt.CommandError(command, "")
        if command[:3] == ["git", "worktree", "list"]:
            return self.worktrees
        if command[:3] == ["git", "worktree", "add"] and self.make_dir:
            Path(command[-2]).mkdir(parents=True)
        return ""

    def commands(self) -> list[list[str]]:
        return [command for command, _, _ in self.calls]


class FakeDatabases:
    def __init__(self, existing: Sequence[str] = (), *, up: bool = True) -> None:
        self.names = set(existing)
        self.up = up
        self.log: list[str] = []

    def _check(self) -> None:
        if not self.up:
            raise wt.DatabaseUnavailableError("connection refused on localhost:5432")

    def exists(self, name: str) -> bool:
        self._check()
        return name in self.names

    def create(self, name: str) -> None:
        self._check()
        self.names.add(name)
        self.log.append(f"create {name}")

    def drop(self, name: str) -> None:
        self._check()
        self.names.discard(name)
        self.log.append(f"drop {name}")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    checkout = tmp_path / "ai-trainer"
    checkout.mkdir()
    (checkout / ".env").write_text(ENV_TEXT)
    return checkout


def _create(root: Path, run: FakeRun, databases: FakeDatabases) -> int:
    return wt.create(351, root=root, run=run, databases=databases, base_url=BASE_URL)


def _mutating(run: FakeRun) -> list[list[str]]:
    reads = (
        ["git", "rev-parse"],
        ["git", "ls-remote"],
        ["git", "worktree", "list"],
        ["gh", "issue"],
    )
    return [c for c in run.commands() if not any(c[: len(r)] == r for r in reads)]


# --- AC1: the branch follows apply-ticket's naming rule and the database is at head ---------


@pytest.mark.parametrize(
    ("number", "title", "branch"),
    [
        (351, TITLE, BRANCH),
        (3, "infra: start the stack with Docker Compose",
         "infra/3-start-stack-with-docker-compose"),
        (350, "infra: block agent commits and pushes to main locally",
         "infra/350-block-agent-commits-and-pushes"),
        (310, "infra: pin one uv version across Docker, CI and uv_build",
         "infra/310-pin-one-uv-version-across"),
        (333, "backend: split draft_cards into smaller steps",
         "backend/333-split-draft-cards-into-smaller-steps"),
        (309, "backend: test Google sign-in through the real Authlib client",
         "backend/309-test-google-sign-in-through-real"),
        (293, "infra: set the app's production telemetry settings at deploy",
         "infra/293-set-apps-production-telemetry-settings"),
    ],
)  # fmt: skip
def test_branch_name_follows_apply_ticket_rule(number: int, title: str, branch: str) -> None:
    area = branch.split("/")[0]
    assert wt.branch_name(number, title, [f"area:{area}", "status:todo"]) == branch


def test_branch_name_needs_an_area_label() -> None:
    with pytest.raises(wt.WorktreeError, match="no area: label"):
        wt.branch_name(351, TITLE, ["status:todo"])


def test_create_runs_steps_in_order(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer"])
    path = root.parent / "ai-trainer-351"

    assert _create(root, run, databases) == 0

    assert _mutating(run) == [
        ["git", "fetch", "origin", "main"],
        ["git", "worktree", "add", "-b", BRANCH, str(path), "origin/main"],
        ["uv", "sync"],
        ["uv", "run", "alembic", "upgrade", "head"],
    ]
    assert databases.log == ["create ai_trainer_wt351"]
    migrate = next(call for call in run.calls if call[0][:3] == ["uv", "run", "alembic"])
    assert migrate[1] == path
    assert migrate[2] == {"DATABASE_URL": WT_URL}
    assert next(call for call in run.calls if call[0] == ["uv", "sync"])[1] == path
    out = capsys.readouterr().out
    assert BRANCH in out
    assert "ai_trainer_wt351" in out


def test_issue_is_read_with_gh(root: Path) -> None:
    run = FakeRun()
    _create(root, run, FakeDatabases())
    assert ["gh", "issue", "view", "351", "--json", "title,labels"] in run.commands()


# --- AC2: tests in the worktree use only its own database -----------------------------------


def test_worktree_env_changes_only_the_database(root: Path) -> None:
    _create(root, FakeRun(), FakeDatabases())

    written = (root.parent / "ai-trainer-351" / ".env").read_text()
    assert written == ENV_TEXT.replace(BASE_URL, WT_URL)


def test_worktree_env_adds_database_url_when_missing() -> None:
    assert wt.worktree_env("LOG_LEVEL=INFO", WT_URL) == f"LOG_LEVEL=INFO\nDATABASE_URL={WT_URL}\n"


def test_worktree_database_url_keeps_credentials_and_host() -> None:
    assert wt.worktree_database_url(BASE_URL, 351) == WT_URL


# --- AC3 + AC4: compose and the Makefile serve the checkout's own database -------------------


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text())
    return loaded


def test_compose_pins_project_name(compose: dict[str, Any]) -> None:
    """`ai-trainer` is the name Compose derived from the main checkout's folder, so the pinned
    name keeps the existing `ai-trainer_postgres_data` volume; without it a worktree folder
    (`ai-trainer-351`) would start a second stack fighting for :8000 and :5432."""
    assert compose["name"] == "ai-trainer"
    assert "postgres_data" in compose["volumes"]
    assert compose["services"]["db"]["volumes"] == ["postgres_data:/var/lib/postgresql"]


@pytest.mark.parametrize(
    ("file", "service"), [("compose.yaml", "app"), ("compose.dev.yaml", "migrate")]
)
def test_app_and_migrate_take_db_from_app_db_name(file: str, service: str) -> None:
    services = yaml.safe_load((ROOT / file).read_text())["services"]
    assert services[service]["environment"]["DATABASE_URL"] == (
        "postgresql+psycopg://ai_trainer:ai_trainer@db:5432/${APP_DB_NAME:-ai_trainer}"
    )


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (f"DATABASE_URL={WT_URL}\n", "ai_trainer_wt351"),
        (f"LOG_LEVEL=INFO\nDATABASE_URL={BASE_URL}\n", "ai_trainer"),
        ("LOG_LEVEL=INFO\n", ""),
        (None, ""),
        (f'DATABASE_URL="{WT_URL}"\n', "ai_trainer_wt351"),
        (f"DATABASE_URL={WT_URL}\nDATABASE_URL={BASE_URL}\n", "ai_trainer"),
    ],
)
def test_makefile_exports_app_db_name_from_env(
    tmp_path: Path, env: str | None, expected: str
) -> None:
    if env is not None:
        (tmp_path / ".env").write_text(env)
    probe = f'include {ROOT}/Makefile\nprobe:\n\t@echo "[$$APP_DB_NAME]"\n'
    (tmp_path / "probe.mk").write_text(probe)

    out = subprocess.run(
        ["make", "-s", "-f", "probe.mk", "probe"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert out.strip() == f"[{expected}]"


# --- AC6 (edge): refuse before creating anything ---------------------------------------------


def test_create_refuses_when_postgres_down(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run, databases = FakeRun(), FakeDatabases(up=False)

    assert _create(root, run, databases) == 1

    assert _mutating(run) == []
    assert not (root.parent / "ai-trainer-351").exists()
    assert "connection refused" in capsys.readouterr().err


def test_create_refuses_when_worktree_exists(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root.parent / "ai-trainer-351").mkdir()
    run, databases = FakeRun(), FakeDatabases()

    assert _create(root, run, databases) == 1

    assert _mutating(run) == []
    assert databases.log == []
    assert "already exists" in capsys.readouterr().err


def test_create_refuses_when_git_still_lists_the_worktree(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A folder deleted by hand stays registered; `git worktree add` would fail after making
    the branch."""
    run, databases = FakeRun(), FakeDatabases()
    run.worktrees = f"worktree {root}\nHEAD abc\n\nworktree {root.parent / 'ai-trainer-351'}\n"

    assert _create(root, run, databases) == 1

    assert _mutating(run) == []
    assert "git worktree prune" in capsys.readouterr().err


def test_create_refuses_when_path_is_a_dangling_symlink(root: Path) -> None:
    (root.parent / "ai-trainer-351").symlink_to(root.parent / "gone")
    run = FakeRun()

    assert _create(root, run, FakeDatabases()) == 1

    assert _mutating(run) == []


def test_create_refuses_when_branch_exists_locally(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases()
    run.local_branches.add(BRANCH)

    assert _create(root, run, databases) == 1

    assert _mutating(run) == []
    assert databases.log == []
    assert f"branch {BRANCH} already exists" in capsys.readouterr().err


def test_create_refuses_when_branch_exists_on_origin(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(remote_branch=True), FakeDatabases()

    assert _create(root, run, databases) == 1

    assert _mutating(run) == []
    assert "on origin" in capsys.readouterr().err


def test_create_refuses_when_database_exists(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer_wt351"])

    assert _create(root, run, databases) == 1

    assert _mutating(run) == []
    assert databases.log == []
    assert "database ai_trainer_wt351 already exists" in capsys.readouterr().err


def test_create_refuses_without_area_label(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = FakeRun(labels=["status:todo"])

    assert _create(root, run, FakeDatabases()) == 1

    assert _mutating(run) == []
    assert "no area: label" in capsys.readouterr().err


def test_create_refuses_when_issue_cannot_be_read(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = FakeRun()
    run.fail[("gh", "issue", "view")] = "Could not resolve to an issue"

    assert _create(root, run, FakeDatabases()) == 1

    assert _mutating(run) == []
    assert "Could not resolve to an issue" in capsys.readouterr().err


def test_create_refuses_without_env(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / ".env").unlink()
    run = FakeRun()

    assert _create(root, run, FakeDatabases()) == 1

    assert run.calls == []
    assert ".env" in capsys.readouterr().err


def test_create_rolls_back_on_late_failure(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run, databases = FakeRun(), FakeDatabases()
    run.fail[("uv", "run", "alembic")] = "alembic: revision not found"
    path = root.parent / "ai-trainer-351"

    assert _create(root, run, databases) == 1

    assert databases.log == ["create ai_trainer_wt351", "drop ai_trainer_wt351"]
    commands = run.commands()
    removed = commands.index(["git", "worktree", "remove", "--force", str(path)])
    assert commands.index(["git", "branch", "-D", BRANCH]) > removed
    assert "revision not found" in capsys.readouterr().err


def test_create_deletes_the_branch_a_failed_worktree_add_made(root: Path) -> None:
    run, databases = FakeRun(), FakeDatabases()
    run.fail[("git", "worktree", "add")] = "fatal: invalid reference"

    assert _create(root, run, databases) == 1

    assert databases.log == []
    assert ["git", "branch", "-D", BRANCH] in run.commands()
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in run.commands())


def test_create_rolls_back_when_branch_was_never_made(root: Path) -> None:
    run = FakeRun()
    run.fail[("git", "fetch")] = "fatal: unable to access origin"

    assert _create(root, run, FakeDatabases()) == 1

    assert ["git", "branch", "-D", BRANCH] not in run.commands()


def test_create_rolls_back_on_a_file_error(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run, databases = FakeRun(), FakeDatabases()
    run.make_dir = False

    assert _create(root, run, databases) == 1

    assert databases.log == []
    assert ["git", "branch", "-D", BRANCH] in run.commands()
    assert "No such file" in capsys.readouterr().err


def test_create_reports_an_undo_step_that_fails(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases()
    run.fail[("uv", "sync")] = "network down"
    run.fail[("git", "worktree", "remove")] = "fatal: locked"

    assert _create(root, run, databases) == 1

    assert databases.log == ["create ai_trainer_wt351", "drop ai_trainer_wt351"]
    assert "could not undo" in capsys.readouterr().err


def test_create_prints_the_worktree_path(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#366: the Claude tab enters the printed path with EnterWorktree; no VS Code window opens."""
    run = FakeRun()

    assert _create(root, run, FakeDatabases()) == 0

    assert not any(command[0] == "code" for command in run.commands())
    assert str(root.parent / "ai-trainer-351") in capsys.readouterr().out


# --- make worktree-prune (#366) ---------------------------------------------------------------


def _worktree_block(path: Path, branch: str) -> str:
    return f"worktree {path}\nHEAD abc123\nbranch refs/heads/{branch}\n\n"


def _prune(root: Path, run: FakeRun, databases: FakeDatabases) -> int:
    return wt.prune(root=root, run=run, databases=databases, base_url=BASE_URL)


TIP = "f00dcafe"


def _merged_pr(number: int = 355, *, head: str = TIP) -> dict[str, Any]:
    return {"number": number, "state": "MERGED", "headRefName": BRANCH, "headRefOid": head}


def _merged_worktree(root: Path, run: FakeRun) -> Path:
    path = root.parent / "ai-trainer-351"
    run.worktrees = _worktree_block(root, "main") + _worktree_block(path, BRANCH)
    run.prs = [_merged_pr()]
    run.tips[BRANCH] = TIP
    return path


def test_prune_removes_a_merged_worktree_its_database_and_branch(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer", "ai_trainer_wt351"])
    path = _merged_worktree(root, run)

    assert _prune(root, run, databases) == 0

    commands = run.commands()
    list_prs = ["gh", "pr", "list", "--state", "all", "--limit", "500", "--json"]
    assert [c[:8] for c in commands if c[:3] == ["gh", "pr", "list"]] == [list_prs]
    removed = commands.index(["git", "worktree", "remove", str(path)])
    assert commands.index(["git", "branch", "-D", BRANCH]) > removed
    assert databases.names == {"ai_trainer"}
    assert "#351" in capsys.readouterr().out


def test_prune_keeps_an_open_pr(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer_wt351"])
    run.worktrees = _worktree_block(root.parent / "ai-trainer-351", BRANCH)
    run.prs = [{"number": 355, "state": "OPEN", "headRefName": BRANCH, "headRefOid": TIP}]

    assert _prune(root, run, databases) == 0

    assert not any(c[:3] == ["git", "worktree", "remove"] for c in run.commands())
    assert databases.names == {"ai_trainer_wt351"}
    assert "PR #355 is OPEN, kept" in capsys.readouterr().out


def test_prune_keeps_a_worktree_without_a_pr(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer_wt351"])
    run.worktrees = _worktree_block(root.parent / "ai-trainer-351", BRANCH)

    assert _prune(root, run, databases) == 0

    assert databases.names == {"ai_trainer_wt351"}
    assert "no PR, kept" in capsys.readouterr().out


def test_prune_skips_a_dirty_merged_worktree(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer_wt351"])
    run.dirty.add(_merged_worktree(root, run))

    assert _prune(root, run, databases) == 0

    assert databases.names == {"ai_trainer_wt351"}
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in run.commands())
    assert "uncommitted changes, kept" in capsys.readouterr().out


def test_prune_keeps_commits_made_after_the_merge(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer_wt351"])
    _merged_worktree(root, run)
    run.tips[BRANCH] = "beefbeef"

    assert _prune(root, run, databases) == 0

    assert ["git", "branch", "-D", BRANCH] not in run.commands()
    assert databases.names == {"ai_trainer_wt351"}
    assert "has commits after it, kept" in capsys.readouterr().out


def test_prune_keeps_the_worktree_make_start_serves(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer_wt351"])
    path = _merged_worktree(root, run)
    run.app_container = f"true {path}\n"

    assert _prune(root, run, databases) == 0

    assert databases.names == {"ai_trainer_wt351"}
    assert "`make stop` first" in capsys.readouterr().out


def test_prune_ignores_worktrees_not_named_like_tickets(root: Path) -> None:
    run = FakeRun()
    run.worktrees = (
        _worktree_block(root, "main")
        + _worktree_block(root / ".claude" / "worktrees" / "spike", "worktree-spike")
        + _worktree_block(root.parent / "ai-trainer-obs", "docs/grafana")
        + f"worktree {root.parent / 'ai-trainer-352'}\nHEAD abc123\ndetached\n\n"
    )
    run.prs = [{"number": 1, "state": "MERGED", "headRefName": "docs/grafana", "headRefOid": "x"}]

    assert _prune(root, run, FakeDatabases()) == 0

    assert _mutating(run) == [
        ["gh", "pr", "list", "--state", "all", "--limit", "500", "--json", wt._PR_FIELDS],
        ["docker", "inspect", "ai-trainer-app-1", "--format",
         f"{{{{.State.Running}}}} {wt._WORKING_DIR}"],
    ]  # fmt: skip


def test_prune_reports_postgres_down_before_touching_git(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = FakeRun()
    _merged_worktree(root, run)

    assert _prune(root, run, FakeDatabases(up=False)) == 1

    assert not any(c[:3] == ["git", "worktree", "remove"] for c in run.commands())
    assert ["git", "branch", "-D", BRANCH] not in run.commands()
    assert "connection refused" in capsys.readouterr().err


def test_prune_reports_a_gh_failure_and_removes_nothing(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer_wt351"])
    _merged_worktree(root, run)
    run.fail[("gh", "pr", "list")] = "HTTP 404: Not Found"

    assert _prune(root, run, databases) == 1

    assert databases.names == {"ai_trainer_wt351"}
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in run.commands())
    assert "can't list PRs" in capsys.readouterr().err


# --- make stack-owner (#366) ------------------------------------------------------------------


def test_stack_owner_reports_another_checkout(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = FakeRun()
    run.toplevel = f"{root}\n"
    run.app_container = f"true {root.parent / 'ai-trainer-351'}\n"

    assert wt.stack_owner(run=run) == 3

    assert capsys.readouterr().out.strip() == f"other {root.parent / 'ai-trainer-351'}"


def test_stack_owner_reports_self(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = FakeRun()
    run.toplevel = f"{root}\n"
    run.app_container = f"true {root}\n"

    assert wt.stack_owner(run=run) == 0

    assert capsys.readouterr().out.strip() == "self"


@pytest.mark.parametrize("label", ["", "<no value>"], ids=["empty", "no-value"])
def test_stack_owner_reports_unknown_without_a_compose_label(
    root: Path, capsys: pytest.CaptureFixture[str], label: str
) -> None:
    run = FakeRun()
    run.toplevel = f"{root}\n"
    run.app_container = f"true {label}\n"

    assert wt.stack_owner(run=run) == 3

    assert capsys.readouterr().out.strip() == "other unknown"


@pytest.mark.parametrize("container", [None, "false /somewhere/else\n"], ids=["absent", "stopped"])
def test_stack_owner_reports_none_when_no_container(
    root: Path, capsys: pytest.CaptureFixture[str], container: str | None
) -> None:
    run = FakeRun()
    run.toplevel = f"{root}\n"
    run.app_container = container

    assert wt.stack_owner(run=run) == 0

    assert capsys.readouterr().out.strip() == "none"


# --- make worktree-rm ------------------------------------------------------------------------


def test_remove_drops_database_after_removing_worktree(root: Path) -> None:
    path = root.parent / "ai-trainer-351"
    run, databases = FakeRun(), FakeDatabases(["ai_trainer", "ai_trainer_wt351"])

    assert wt.remove(351, root=root, run=run, databases=databases, base_url=BASE_URL) == 0

    assert run.commands() == [["git", "worktree", "remove", str(path)]]
    assert databases.names == {"ai_trainer"}


def test_remove_keeps_database_when_worktree_is_dirty(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run, databases = FakeRun(), FakeDatabases(["ai_trainer_wt351"])
    run.fail[("git", "worktree", "remove")] = "fatal: contains modified or untracked files"

    assert wt.remove(351, root=root, run=run, databases=databases, base_url=BASE_URL) == 1

    assert databases.names == {"ai_trainer_wt351"}
    assert "modified or untracked" in capsys.readouterr().err


def test_remove_reports_postgres_down(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = FakeRun()

    assert (
        wt.remove(351, root=root, run=run, databases=FakeDatabases(up=False), base_url=BASE_URL)
        == 1
    )

    assert "connection refused" in capsys.readouterr().err


# --- the real command runner, the main checkout and the CLI ----------------------------------


def test_subprocess_run_returns_stdout_and_raises_with_stderr(tmp_path: Path) -> None:
    assert wt.subprocess_run(["sh", "-c", "echo $DATABASE_URL"], env={"DATABASE_URL": "x"}) == "x\n"
    assert wt.subprocess_run(["pwd"], cwd=tmp_path).strip() == str(tmp_path.resolve())
    with pytest.raises(wt.CommandError, match="boom"):
        wt.subprocess_run(["sh", "-c", "echo boom >&2; exit 3"])
    with pytest.raises(wt.CommandError, match="not found"):
        wt.subprocess_run(["no-such-command-351"])


def test_subprocess_run_drops_the_callers_virtualenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """`uv sync` in the worktree must build its own `.venv`, not the main checkout's."""
    monkeypatch.setenv("VIRTUAL_ENV", "/main/.venv")
    assert wt.subprocess_run(["sh", "-c", "echo [${VIRTUAL_ENV:-}]"]).strip() == "[]"


def test_main_checkout_is_found_from_a_worktree(tmp_path: Path) -> None:
    def run(args: Sequence[str], **_: Any) -> str:
        assert list(args) == ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
        return f"{tmp_path}/ai-trainer/.git\n"

    assert wt.main_checkout(run) == tmp_path / "ai-trainer"


@pytest.mark.parametrize("command", ["create", "remove"])
def test_main_dispatches(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    seen: dict[str, Any] = {}

    def fake(number: int, **kwargs: Any) -> int:
        seen.update(kwargs, number=number)
        return 7

    monkeypatch.setattr(wt, command, fake)
    monkeypatch.setattr(wt, "main_checkout", lambda run: Path("/repo"))
    monkeypatch.setattr(wt, "shared_database_url", lambda root: BASE_URL)

    assert wt.main([command, "351"]) == 7
    assert seen["number"] == 351
    assert seen["root"] == Path("/repo")
    assert seen["base_url"] == BASE_URL
    assert isinstance(seen["databases"], wt.PostgresDatabases)


def test_main_dispatches_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 5

    monkeypatch.setattr(wt, "prune", fake)
    monkeypatch.setattr(wt, "main_checkout", lambda run: Path("/repo"))
    monkeypatch.setattr(wt, "shared_database_url", lambda root: BASE_URL)

    assert wt.main(["prune"]) == 5
    assert seen["root"] == Path("/repo")
    assert isinstance(seen["databases"], wt.PostgresDatabases)


def test_main_dispatches_stack_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wt, "stack_owner", lambda run: 3)
    assert wt.main(["stack-owner"]) == 3


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["create"], "needs an issue number"),
        (["remove"], "needs an issue number"),
        (["prune", "351"], "takes no issue number"),
        (["stack-owner", "351"], "takes no issue number"),
    ],
)
def test_main_checks_the_issue_argument_first(
    argv: list[str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A usage error, never a git or Settings error from a checkout without a valid `.env`."""

    def unreachable(*_: Any) -> Path:
        raise AssertionError("argument checks come first")

    monkeypatch.setattr(wt, "main_checkout", unreachable)
    with pytest.raises(SystemExit) as exit_info:
        wt.main(argv)
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_main_rejects_a_non_numeric_issue(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        wt.main(["create", "351; rm -rf /"])
    assert exit_info.value.code == 2


def test_shared_database_url_reads_the_main_checkouts_env(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Called from a worktree, whose `.env` names `ai_trainer_wt<n>`, it still reads the main
    checkout's, so the next worktree's database isn't `ai_trainer_wt351_wt<m>`."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "test/judge-model")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text(f"DATABASE_URL={WT_URL}\n")
    monkeypatch.chdir(elsewhere)

    assert wt.shared_database_url(root) == BASE_URL
