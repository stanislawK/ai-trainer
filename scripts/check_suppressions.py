"""Fails on a lint, type or coverage suppression that carries no code or no reason (ADR-0002).

Run with `uv run python scripts/check_suppressions.py`. It scans `src/` and `scripts/` and accepts
only these forms:

- `# type: ignore[<code>]  # <reason>`
- `# noqa: <code> -- <reason>`
- `# pragma: no cover -- <reason>`

Only comments are read (through `tokenize`), so the same text inside a string literal is not a
suppression and is never flagged.
"""

import io
import re
import sys
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Each directive must open a comment or follow a `#`, so prose that merely mentions one is not read
# as a suppression.
_TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore(?:\[(?P<code>[^\]]*)\])?(?P<rest>.*)")
_RULE = r"[A-Za-z]+[0-9]+"
_NOQA = re.compile(rf"#\s*noqa\b(?::\s*(?P<code>{_RULE}(?:[\s,]+{_RULE})*))?(?P<rest>.*)")
_PRAGMA = re.compile(r"#\s*pragma:\s*no cover\b(?P<rest>.*)")
_TYPE_IGNORE_FORM = "# type: ignore[<code>]  # <reason>"
_DASH_REASON = re.compile(r"^\s*--\s*\S")
_HASH_REASON = re.compile(r"^\s*#\s*\S")


@dataclass(frozen=True, slots=True)
class SuppressionViolation:
    path: Path
    line: int
    message: str


def find_violations(root: Path) -> list[SuppressionViolation]:
    if not root.is_dir():
        # A root that is not there would scan nothing and pass, e.g. run from another directory.
        raise FileNotFoundError(f"{root} is not a directory")
    violations: list[SuppressionViolation] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                for problem in _problems(token.string):
                    violations.append(
                        SuppressionViolation(
                            path=path,
                            line=token.start[0],
                            message=f"{path}:{token.start[0]}: {problem}",
                        )
                    )
    return violations


def _problems(comment: str) -> list[str]:
    problems: list[str] = []

    type_ignore = _TYPE_IGNORE.search(comment)
    if type_ignore:
        if not (type_ignore["code"] or "").strip():
            problems.append(f"`type: ignore` needs an error code: `{_TYPE_IGNORE_FORM}`")
        elif not _HASH_REASON.match(type_ignore["rest"]):
            problems.append(f"`type: ignore[...]` needs a reason: `{_TYPE_IGNORE_FORM}`")

    noqa = _NOQA.search(comment)
    if noqa:
        if not noqa["code"]:
            problems.append("`noqa` needs a rule code: `# noqa: <code> -- <reason>`")
        elif not _DASH_REASON.match(noqa["rest"]):
            problems.append("`noqa` needs a reason: `# noqa: <code> -- <reason>`")

    pragma = _PRAGMA.search(comment)
    if pragma and not _DASH_REASON.match(pragma["rest"]):
        problems.append("`pragma: no cover` needs a reason: `# pragma: no cover -- <reason>`")

    return problems


DEFAULT_ROOTS = (Path("src"), Path("scripts"))


def main(roots: Sequence[Path] = DEFAULT_ROOTS) -> int:
    violations = [violation for root in roots for violation in find_violations(root)]
    for violation in violations:
        print(violation.message)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover -- only runs via direct script execution, not import
