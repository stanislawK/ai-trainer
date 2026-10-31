"""Start, remove or prune a ticket's own checkout and database (ADR-0035 parallel lanes, #351,
#366).

Run with `make worktree issue=<n>`, `make worktree-rm issue=<n>`, `make worktree-prune` and
`make stack-owner`, or `uv run python -m scripts.worktree create|remove <n>` / `prune` /
`stack-owner`.

`create` makes `../ai-trainer-<n>` on `<area>/<n>-<slug>` from `origin/main`, copies `.env` with
`DATABASE_URL` pointing at `ai_trainer_wt<n>`, creates and migrates that database, runs
`uv sync` and prints the path, which the ticket's Claude tab then enters with `EnterWorktree`.
Every check runs before anything is made; a step that fails afterwards undoes the earlier ones.
`remove` removes the worktree (git refuses while it has uncommitted changes) and then drops its
database; the branch stays for its PR. `prune` does the same for every ticket worktree whose PR
is merged, and also deletes its branch. `stack-owner` says which checkout `make start` serves.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

# `apply-ticket`'s own example, "start the stack with Docker Compose" →
# `start-stack-with-docker-compose`, leaves out only the articles.
_ARTICLES = frozenset({"a", "an", "the"})
_SLUG_WORDS = 5


class WorktreeError(Exception):
    """A reason to stop, printed as is."""


class CommandError(WorktreeError):
    def __init__(self, args: Sequence[str], output: str) -> None:
        super().__init__(f"`{' '.join(args)}` failed: {output.strip()}")


class DatabaseUnavailableError(WorktreeError):
    pass


class Run(Protocol):
    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> str: ...


class Databases(Protocol):
    def exists(self, name: str) -> bool: ...
    def create(self, name: str) -> None: ...
    def drop(self, name: str) -> None: ...


def subprocess_run(
    args: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> str:
    """Runs a command and returns its stdout; `env` is added to this process's environment."""
    # Without this `uv sync` in the worktree would reuse the main checkout's virtualenv.
    full_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"} | dict(env or {})
    try:
        done = subprocess.run(
            list(args), cwd=cwd, env=full_env, capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        raise CommandError(args, f"{args[0]}: command not found") from None
    if done.returncode != 0:
        raise CommandError(args, done.stderr or done.stdout)
    return done.stdout


class PostgresDatabases:
    """Databases on the server the shared one lives on, reached through the shared one."""

    def __init__(self, shared_url: str) -> None:
        self._dsn = to_psycopg_dsn(shared_url)

    def _execute(self, statement: sql.SQL | sql.Composed, params: Sequence[str] = ()) -> bool:
        try:
            # CREATE/DROP DATABASE can't run inside a transaction block, hence autocommit.
            with psycopg.connect(self._dsn, autocommit=True, connect_timeout=5) as conn:
                cursor = conn.execute(statement, params)
                return cursor.description is not None and cursor.fetchone() is not None
        except psycopg.OperationalError as error:
            raise DatabaseUnavailableError(f"Postgres is not reachable: {error}") from None
        except psycopg.Error as error:
            raise WorktreeError(f"Postgres refused: {error}") from None

    def exists(self, name: str) -> bool:
        return self._execute(sql.SQL("SELECT 1 FROM pg_database WHERE datname = %s"), [name])

    def create(self, name: str) -> None:
        self._execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    def drop(self, name: str) -> None:
        self._execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def branch_name(number: int, title: str, labels: Sequence[str]) -> str:
    """`<area>/<n>-<slug>`: the slug is the title without its `area:` prefix, lowercase
    kebab-case, at most five words (`apply-ticket` step 6)."""
    area = next((label.removeprefix("area:") for label in labels if label.startswith("area:")), "")
    if not area:
        raise WorktreeError(f"issue #{number} has no area: label")
    # A word is what spaces separate, so "sign-in" stays one word and "app's" becomes "apps".
    words = (
        re.sub(r"[^a-z0-9]+", "-", w.replace("'", "")).strip("-")
        for w in title.split(":", 1)[-1].lower().split()
    )
    kept = [word for word in words if word and word not in _ARTICLES][:_SLUG_WORDS]
    return f"{area}/{number}-{'-'.join(kept)}"


def worktree_database_url(base_url: str, number: int) -> str:
    base = make_url(base_url)
    return base.set(database=f"{base.database}_wt{number}").render_as_string(hide_password=False)


def worktree_env(env_text: str, database_url: str) -> str:
    """The main checkout's `.env` with only `DATABASE_URL` changed."""
    lines = env_text.splitlines()
    line = f"DATABASE_URL={database_url}"
    if any(old.startswith("DATABASE_URL=") for old in lines):
        lines = [line if old.startswith("DATABASE_URL=") else old for old in lines]
    else:
        lines.append(line)
    return "\n".join(lines) + "\n"


def _worktree_path(root: Path, number: int) -> Path:
    return root.parent / f"{root.name}-{number}"


def _branch_exists_locally(run: Run, branch: str) -> bool:
    try:
        run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    except CommandError:
        return False
    return True


def _preflight(
    number: int, *, root: Path, run: Run, databases: Databases, base_url: str
) -> tuple[Path, str, str, str]:
    """Every reason not to start, checked before anything is made."""
    env_file = root / ".env"
    if not env_file.is_file():
        raise WorktreeError(f"{env_file} is missing; copy .env.example to .env first")
    path = _worktree_path(root, number)
    if path.exists() or path.is_symlink():
        raise WorktreeError(f"{path} already exists")
    if f"worktree {path}\n" in run(["git", "worktree", "list", "--porcelain"]):
        raise WorktreeError(f"git still lists {path} as a worktree; run `git worktree prune`")
    issue = json.loads(run(["gh", "issue", "view", str(number), "--json", "title,labels"]))
    branch = branch_name(number, issue["title"], [label["name"] for label in issue["labels"]])
    if _branch_exists_locally(run, branch):
        raise WorktreeError(f"branch {branch} already exists")
    if run(["git", "ls-remote", "--heads", "origin", branch]).strip():
        raise WorktreeError(f"branch {branch} already exists on origin")
    database_url = worktree_database_url(base_url, number)
    database = make_url(database_url).database or ""
    if databases.exists(database):
        raise WorktreeError(f"database {database} already exists")
    return path, branch, database_url, database


def create(number: int, *, root: Path, run: Run, databases: Databases, base_url: str) -> int:
    try:
        path, branch, database_url, database = _preflight(
            number, root=root, run=run, databases=databases, base_url=base_url
        )
    except WorktreeError as error:
        print(f"worktree: {error}; nothing was created", file=sys.stderr)
        return 1

    undo: list[Callable[[], object]] = []
    try:
        run(["git", "fetch", "origin", "main"])
        # A failing `git worktree add -b` may already have made the branch.
        undo.append(
            lambda: _branch_exists_locally(run, branch) and run(["git", "branch", "-D", branch])
        )
        run(["git", "worktree", "add", "-b", branch, str(path), "origin/main"])
        undo.append(lambda: run(["git", "worktree", "remove", "--force", str(path)]))
        (path / ".env").write_text(worktree_env((root / ".env").read_text(), database_url))
        databases.create(database)
        undo.append(lambda: databases.drop(database))
        run(["uv", "sync"], cwd=path)
        run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=path,
            env={"DATABASE_URL": database_url},
        )
    except (WorktreeError, OSError) as error:
        print(f"worktree: {error}; undoing what was made", file=sys.stderr)
        for step in reversed(undo):
            try:
                step()
            except WorktreeError as undo_error:
                print(f"worktree: could not undo: {undo_error}", file=sys.stderr)
        return 1

    print(f"worktree: {path} on {branch}, database {database} at head")
    return 0


def remove(number: int, *, root: Path, run: Run, databases: Databases, base_url: str) -> int:
    path = _worktree_path(root, number)
    database = make_url(worktree_database_url(base_url, number)).database or ""
    try:
        run(["git", "worktree", "remove", str(path)])
        databases.drop(database)
    except WorktreeError as error:
        print(f"worktree: {error}", file=sys.stderr)
        return 1
    print(f"worktree: removed {path} and dropped {database}; the branch stays")
    return 0


def _ticket_worktrees(root: Path, run: Run) -> list[tuple[int, Path, str]]:
    """`(n, path, branch)` for each `../ai-trainer-<n>` worktree on a branch."""
    pattern = re.compile(rf"{re.escape(root.name)}-(\d+)")
    found = []
    for block in run(["git", "worktree", "list", "--porcelain"]).split("\n\n"):
        fields = dict(line.partition(" ")[::2] for line in block.splitlines())
        path = Path(fields.get("worktree", ""))
        match = pattern.fullmatch(path.name)
        if match and path.parent == root.parent and "branch" in fields:
            found.append((int(match[1]), path, fields["branch"].removeprefix("refs/heads/")))
    return found


_PR_FIELDS = "number,state,headRefName,headRefOid"
_WORKING_DIR = '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'


def _served_checkout(run: Run) -> str | None:
    """The checkout the running `make start` app serves, `unknown` when its compose label is
    missing, or None when no app container runs."""
    try:
        state = run(
            [
                "docker",
                "inspect",
                "ai-trainer-app-1",
                "--format",
                f"{{{{.State.Running}}}} {_WORKING_DIR}",
            ]
        )
    except CommandError:
        return None
    running, _, served = state.strip().partition(" ")
    if running != "true":
        return None
    return served if served and served != "<no value>" else "unknown"


def _keep_reason(
    path: Path, branch: str, prs: Sequence[Mapping[str, str]], served: str | None, run: Run
) -> str | None:
    """Why a ticket worktree stays, or None when it can go: its PR is merged, the merged head is
    still the branch tip, nothing is uncommitted and `make start` doesn't serve it."""
    merged = next((pr for pr in prs if pr["state"] == "MERGED"), None)
    if merged is None:
        return f"PR #{prs[0]['number']} is {prs[0]['state']}" if prs else "no PR"
    if served is not None and served != "unknown" and Path(served).resolve() == path.resolve():
        return f"PR #{merged['number']} merged, but `make start` serves it; run `make stop` first"
    if run(["git", "status", "--porcelain"], cwd=path).strip():
        return f"PR #{merged['number']} merged, but it has uncommitted changes"
    if run(["git", "rev-parse", f"refs/heads/{branch}"]).strip() != merged["headRefOid"]:
        return f"PR #{merged['number']} merged, but {branch} has commits after it"
    return None


def prune(*, root: Path, run: Run, databases: Databases, base_url: str) -> int:
    """Removes every ticket worktree whose PR is merged, with its database and branch."""
    try:
        listed = json.loads(
            run(["gh", "pr", "list", "--state", "all", "--limit", "500", "--json", _PR_FIELDS])
        )
    except WorktreeError as error:
        print(f"worktree: can't list PRs: {error}; nothing was removed", file=sys.stderr)
        return 1
    prs_by_branch: dict[str, list[dict[str, str]]] = {}
    for pr in listed:
        prs_by_branch.setdefault(pr["headRefName"], []).append(pr)
    served = _served_checkout(run)
    failed = False
    for number, path, branch in _ticket_worktrees(root, run):
        prs = prs_by_branch.get(branch, [])
        reason = _keep_reason(path, branch, prs, served, run)
        if reason is not None:
            print(f"worktree: #{number}: {reason}, kept")
            continue
        database = make_url(worktree_database_url(base_url, number)).database or ""
        try:
            databases.exists(database)  # fails fast while Postgres is down, before git changes
            run(["git", "worktree", "remove", str(path)])
            databases.drop(database)
            run(["git", "branch", "-D", branch])
        except WorktreeError as error:
            print(f"worktree: #{number}: {error}", file=sys.stderr)
            failed = True
            continue
        print(f"worktree: #{number}: PR merged; removed {path}, {database}, {branch}")
    return 1 if failed else 0


def stack_owner(*, run: Run) -> int:
    """Prints `none`, `self` or `other <path>` for the checkout `make start` serves; 3 on other."""
    here = Path(run(["git", "rev-parse", "--show-toplevel"]).strip())
    served = _served_checkout(run)
    if served is None:
        print("none")
        return 0
    if served != "unknown" and Path(served).resolve() == here.resolve():
        print("self")
        return 0
    print(f"other {served}")
    return 3


def main_checkout(run: Run) -> Path:
    """The main checkout's root, also when called from inside a worktree."""
    common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    return Path(common.strip()).parent


def shared_database_url(root: Path) -> str:
    """The main checkout's database, also when called from inside a worktree."""
    return str(Settings(_env_file=root / ".env").database_url)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "remove", "prune", "stack-owner"])
    parser.add_argument("issue", type=int, nargs="?")
    args = parser.parse_args(argv)
    per_issue = args.command in ("create", "remove")
    if per_issue and args.issue is None:
        parser.error(f"{args.command} needs an issue number")
    if not per_issue and args.issue is not None:
        parser.error(f"{args.command} takes no issue number")
    if args.command == "stack-owner":
        return stack_owner(run=subprocess_run)
    root = main_checkout(subprocess_run)
    base_url = shared_database_url(root)
    databases = PostgresDatabases(base_url)
    if args.command == "prune":
        return prune(root=root, run=subprocess_run, databases=databases, base_url=base_url)
    action = create if args.command == "create" else remove
    return action(args.issue, root=root, run=subprocess_run, databases=databases, base_url=base_url)


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover -- only runs via direct script execution, not import
