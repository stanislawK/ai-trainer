"""Decide which CI jobs a pull request needs from its changed paths (ADR-0013, #357).

Run by the `changes` job in `ci.yml` as
`uv run --no-project python scripts/ci_changes.py --base origin/<base>` on a pull request, and with
`--all` on a push to `main`. It prints `static=`, `test=`, `ops=` and `e2e=` lines for
`$GITHUB_OUTPUT`, and each job skips itself through a job-level `if:` on them.

The rules name what is safe to skip, not what triggers a run, so a path no rule names runs every
job. An empty diff runs every job too.
"""

import argparse
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

_REPO_ROOT = Path(__file__).resolve().parent.parent

JOBS = ("static", "test", "ops", "e2e")

# A pull request touching only these skips every job but the `ci` gate.
_SKIPPABLE = ("docs/**", "README.md", "CLAUDE.md", ".claude/rules/**", ".claude/skills/**")
_NOT_SKIPPABLE = ("docs/api/openapi.json",)

_OPS = (
    "deploy/**",
    "scripts/**",
    "compose*.yaml",
    "Dockerfile",
    "Makefile",
    "tests/ops/**",
    "tests/conftest.py",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/**",
)
_E2E = (
    "src/**",
    "migrations/**",
    "tests/e2e/**",
    "tests/conftest.py",
    "Dockerfile",
    "compose.yaml",
    ".env.example",
    "Makefile",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/**",
)
# Known to need only `static` and `test`, beyond what the `ops` and `e2e` lists name.
_STATIC_TEST_ONLY = (
    "src/**",
    "migrations/**",
    "tests/**",
    "evals/**",
    ".claude/hooks/**",
    "docs/api/openapi.json",
)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    pure = PurePosixPath(path)
    return any(pure.full_match(pattern) for pattern in patterns)


def flags(paths: Sequence[str]) -> dict[str, bool]:
    """Which jobs the changed `paths` need; every job when a path is unknown or none changed."""
    if not paths:
        return dict.fromkeys(JOBS, True)
    needed = dict.fromkeys(JOBS, False)
    for path in paths:
        if _matches(path, _SKIPPABLE) and not _matches(path, _NOT_SKIPPABLE):
            continue
        ops, e2e = _matches(path, _OPS), _matches(path, _E2E)
        if not (ops or e2e or _matches(path, _STATIC_TEST_ONLY)):
            return dict.fromkeys(JOBS, True)
        needed["static"] = needed["test"] = True
        needed["ops"] |= ops
        needed["e2e"] |= e2e
    return needed


def changed_paths(base: str, repo_root: Path = _REPO_ROOT) -> list[str]:
    """Paths changed on this branch since it left `base`, both sides of a rename included."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def output_lines(needed: dict[str, bool]) -> list[str]:
    return [f"{job}={str(needed[job]).lower()}" for job in JOBS]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--base", help="the pull request's base ref, e.g. origin/main")
    which.add_argument("--all", action="store_true", help="run every job (a push to main)")
    parser.add_argument("--root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)
    needed = dict.fromkeys(JOBS, True) if args.all else flags(changed_paths(args.base, args.root))
    print("\n".join(output_lines(needed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover -- only runs via direct script execution, not import
