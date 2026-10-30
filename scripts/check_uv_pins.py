"""Fail when the uv pins disagree (ADR-0002, ADR-0013).

Run with `make uv-pins-check` or `uv run python scripts/check_uv_pins.py [--root <dir>]`.
uv is pinned in the Dockerfile, in each `setup-uv` step of `ci.yml` and `evals.yml`, and as the
floor of the `uv_build` range in `pyproject.toml`. All of them must read the same version, and
the range's upper bound must be the next minor release. A mismatch passes CI otherwise, because
uv then downloads the build backend from PyPI.
"""

import argparse
import re
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_DOCKERFILE = "Dockerfile"
_WORKFLOWS = (".github/workflows/ci.yml", ".github/workflows/evals.yml")
_PYPROJECT = "pyproject.toml"

_IMAGE_PIN = re.compile(r"ghcr\.io/astral-sh/uv:(?P<v>\d+\.\d+\.\d+)")
_SETUP_UV = re.compile(r"uses:\s*astral-sh/setup-uv@")
_ACTION_VERSION = re.compile(r"^\s*version:\s*[\"']?(?P<v>\d+\.\d+\.\d+)[\"']?\s*(#.*)?$")
_BACKEND = re.compile(r"uv_build\s*>=\s*(?P<low>\d+\.\d+\.\d+)\s*,\s*<\s*(?P<high>\d+\.\d+\.\d+)")


def _code(path: Path) -> str:
    """The file's text without whole-line comments, so a commented-out pin is never read."""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def _setup_uv_versions(text: str) -> list[str | None]:
    """One entry per `setup-uv` step: its `version:` input, or None when it has none."""
    lines = text.splitlines()
    found: list[str | None] = []
    for index, line in enumerate(lines):
        if not _SETUP_UV.search(line):
            continue
        version = None
        for following in lines[index + 1 :]:
            if following.lstrip().startswith("- "):
                break
            match = _ACTION_VERSION.match(following)
            if match:
                version = match["v"]
                break
        found.append(version)
    return found


def _next_minor(version: str) -> str:
    major, minor, _ = version.split(".")
    return f"{major}.{int(minor) + 1}.0"


Pins = list[tuple[str, str]]


def _dockerfile_pin(root: Path, pins: Pins, problems: list[str]) -> None:
    images = _IMAGE_PIN.findall(_code(root / _DOCKERFILE))
    if images:
        pins.extend((_DOCKERFILE, version) for version in images)
    else:
        problems.append(f"{_DOCKERFILE}: no uv pin (ghcr.io/astral-sh/uv:<version>) found")


def _workflow_pins(root: Path, pins: Pins, problems: list[str]) -> None:
    for workflow in _WORKFLOWS:
        versions = _setup_uv_versions(_code(root / workflow))
        if not versions:
            problems.append(f"{workflow}: no setup-uv step found")
        for version in versions:
            if version is None:
                problems.append(f"{workflow}: a setup-uv step has no pinned version")
            else:
                pins.append((workflow, version))


def _backend_pin(root: Path, pins: Pins, problems: list[str]) -> None:
    backends = list(_BACKEND.finditer(_code(root / _PYPROJECT)))
    if not backends:
        problems.append(f"{_PYPROJECT}: no uv_build>=X.Y.Z,<X.Y+1.0 range found")
    for backend in backends:
        pins.append((_PYPROJECT, backend["low"]))
        if backend["high"] != _next_minor(backend["low"]):
            problems.append(
                f"{_PYPROJECT}: uv_build upper bound <{backend['high']} should be "
                f"<{_next_minor(backend['low'])}"
            )


def check(root: Path) -> list[str]:
    """Return one message per problem; empty when every pin agrees."""
    pins: Pins = []
    problems: list[str] = []
    _dockerfile_pin(root, pins, problems)
    _workflow_pins(root, pins, problems)
    _backend_pin(root, pins, problems)
    if pins:
        expected = Counter(version for _, version in pins).most_common(1)[0][0]
        problems.extend(
            f"{file}: uv {version} differs from the other pins ({expected})"
            for file, version in pins
            if version != expected
        )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)
    problems = check(args.root)
    if problems:
        print("uv pins disagree:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"uv pins agree: {_pinned_version(args.root)}")
    return 0


def _pinned_version(root: Path) -> str:
    images = _IMAGE_PIN.findall(_code(root / _DOCKERFILE))
    return images[0] if images else "?"


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover -- only runs via direct script execution, not import
