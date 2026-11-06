"""The `xdist_group` marks `tests/conftest.py` sets keep a group on one worker (ADR-0013, #328).

pytest 9.2 keeps node IDs in a structured `NodeId` (pytest #14758), and pytest-xdist 3.8.0 still
writes its group suffix to the old `_nodeid`, so the group silently fell apart (#484). This runs
the `src_tree` group on two workers in a subprocess and fails if its tests are split. The test
itself is in `src_tree`, so its inner run never writes into `src/` beside the outer one.
"""

import re
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_TREE_FILES = ("tests/unit/test_import_boundaries.py", "tests/unit/test_clock_usage.py")
_RESULT_LINE = re.compile(r"^\[(gw\d+)\] \[ *\d+%\] (?:PASSED|FAILED) (\S+)", re.MULTILINE)


def _xdist_writes_the_suffix() -> bool:
    """pytest-xdist up to 3.8.0 marks a grouped node ID with `@<group>`; its #1388 drops that."""
    major, minor = (int(part) for part in version("pytest-xdist").split(".")[:2])
    return (major, minor) <= (3, 8)


def test_a_group_runs_on_one_worker() -> None:
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *SRC_TREE_FILES,
            "-n",
            "2",
            "-v",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    results = _RESULT_LINE.findall(run.stdout)
    assert len({worker for worker, _ in results}) == 1, run.stdout
    if _xdist_writes_the_suffix():
        assert all(node_id.endswith("@src_tree") for _, node_id in results), run.stdout
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(results) >= 5, run.stdout
