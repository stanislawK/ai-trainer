"""Lint the SQL of migration revisions new on this branch with squawk (ADR-0024).

Run with `make squawk` or `uv run python scripts/squawk_migrations.py [--base origin/main]`.
Each revision added since the base ref is rendered with Alembic's offline mode
(`alembic upgrade <down_revision>:<revision> --sql`) and piped to squawk with its default rules
(minus the two timeout rules named below).

A deliberate contract step carries a comment in its revision file:

    # squawk-ignore ban-drop-column: <reason> (#<ticket>)

Python comments never reach the rendered SQL, so this script reads the comment and tells squawk
to skip those rules for that revision. A `squawk-ignore` comment without a reason and a ticket
fails the step, and so does a revision that cannot render offline: nothing is skipped silently.
"""

import argparse
import ast
import re
import shutil
import subprocess
import sys
import tokenize
from collections.abc import Callable, Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERSIONS = "migrations/versions"
_IGNORE_LINE = re.compile(r"#\s*squawk-ignore\b")
_IGNORE_FULL = re.compile(
    r"#\s*squawk-ignore\s+(?P<rules>[a-z0-9-]+(?:\s*,\s*[a-z0-9-]+)*)\s*:\s*"
    r"(?P<reason>\S.*?)\s*\(#(?P<ticket>\d+)\)\s*$"
)

# Squawk's default rules, minus two that fire on every ALTER of an existing table (ADR-0024
# Consequences): migrations run in a pre-deploy step, and a timeout there is its own decision.
_EXCLUDED_RULES = ("require-lock-timeout", "require-statement-timeout")

Renderer = Callable[[str, str], str]


class RenderError(Exception):
    """A revision could not be rendered to SQL offline."""


class IgnoreError(Exception):
    """A `squawk-ignore` comment is malformed."""


def new_revision_files(base: str, repo_root: Path = _REPO_ROOT) -> list[Path]:
    """Revision files added on this branch since it left `base`, oldest name first."""
    out = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACR",
            f"{base}...HEAD",
            "--",
            f"{_VERSIONS}/*.py",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [repo_root / line for line in sorted(out.splitlines()) if line]


def _module_value(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            return ast.literal_eval(node.value) if node.value else None
    return None


def revision_ids(path: Path) -> tuple[str, str]:
    """`(down_revision, revision)` of a revision file; `base` when it has no parent."""
    try:
        tree = ast.parse(path.read_text())
        revision = _module_value(tree, "revision")
        down = _module_value(tree, "down_revision")
    except (SyntaxError, ValueError) as exc:
        raise RenderError(f"{path.name}: cannot read its revision ids ({exc})") from exc
    if not isinstance(revision, str):
        raise RenderError(f"{path.name}: no string `revision`")
    if down is None:
        return "base", revision
    if not isinstance(down, str):
        raise RenderError(f"{path.name}: needs a single down_revision, not a merge ({down!r})")
    return down, revision


def ignored_rules(path: Path) -> list[str]:
    """Rules named by the revision's `squawk-ignore` comments, each with a reason and ticket."""
    rules: list[str] = []
    with path.open("rb") as handle:
        comments = [
            (token.start[0], token.string)
            for token in tokenize.tokenize(handle.readline)
            if token.type == tokenize.COMMENT
        ]
    for number, comment in comments:
        if not _IGNORE_LINE.search(comment):
            continue
        match = _IGNORE_FULL.search(comment)
        if match is None:
            raise IgnoreError(
                f"{path.name}:{number}: squawk-ignore needs rule(s), a reason and a ticket, "
                "e.g. `# squawk-ignore ban-drop-column: <reason> (#123)`"
            )
        rules += [rule.strip() for rule in match["rules"].split(",")]
    return rules


def render_offline(down_revision: str, revision: str) -> str:
    """The SQL Alembic emits for `down_revision:revision`, without a database connection."""
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", f"{down_revision}:{revision}", "--sql"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RenderError(proc.stderr.strip() or f"alembic exited {proc.returncode}")
    if not _has_schema_sql(proc.stdout):
        raise RenderError(
            "it renders no SQL besides the alembic_version bookkeeping, so there is nothing "
            "to lint. A body guarded by `context.is_offline_mode()` hides its statements."
        )
    return proc.stdout


def _has_schema_sql(sql: str) -> bool:
    """True when the output has a statement other than transaction and version bookkeeping."""
    for raw in sql.split(";"):
        statement = "\n".join(
            line for line in raw.splitlines() if line.strip() and not line.startswith("--")
        ).strip()
        if not statement or statement.upper() in {"BEGIN", "COMMIT"}:
            continue
        if "alembic_version" not in statement.split("(")[0]:
            return True
    return False


def _squawk(sql: str, name: str) -> int:
    executable = shutil.which("squawk")
    if executable is None:
        print(
            "error: squawk is not on PATH; run through `uv run` or `make squawk`", file=sys.stderr
        )
        return 1
    proc = subprocess.run(
        [executable, "--exclude", ",".join(_EXCLUDED_RULES), "--stdin-filepath", name],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def lint_revisions(files: Sequence[Path], render: Renderer = render_offline) -> int:
    if not files:
        print("No new migration revisions; nothing to lint.")
        return 0
    failed = False
    for path in files:
        try:
            down, revision = revision_ids(path)
            ignored = ignored_rules(path)
            sql = render(down, revision)
        except IgnoreError as exc:
            print(f"error: {exc}", file=sys.stderr)
            failed = True
            continue
        except RenderError as exc:
            print(
                f"error: revision {path.name} cannot be rendered offline, so it was not "
                f"linted. Guard its Python with `context.is_offline_mode()` or fix it.\n{exc}",
                file=sys.stderr,
            )
            failed = True
            continue
        if ignored:
            sql = f"-- squawk-ignore-file {', '.join(ignored)}\n{sql}"
        print(f"squawk: {path.name} ({down} -> {revision})")
        if (
            _squawk(
                sql,
                str(path.relative_to(_REPO_ROOT)) if path.is_relative_to(_REPO_ROOT) else path.name,
            )
            != 0
        ):
            failed = True
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--base", default="origin/main", help="ref the branch is compared with")
    args = parser.parse_args(argv)
    try:
        files = new_revision_files(args.base)
    except subprocess.CalledProcessError as exc:
        print(
            f"error: cannot diff against {args.base!r} ({exc.stderr.strip()}). "
            "Fetch it first, or pass --base <ref>.",
            file=sys.stderr,
        )
        return 1
    return lint_revisions(files)


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover -- only runs via direct script execution, not import
