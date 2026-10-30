"""`ci.yml` runs its checks as parallel jobs behind one `ci` gate (tickets #356 and #357, ADR-0013
"Parallel, path-aware CI"): `changes` decides which of `static`, `test`, `ops` and `e2e` a pull
request needs, those start together after it, and `ci`, the check `main`'s ruleset requires, fails
when any job failed or was cancelled."""

import shlex
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHECK_JOBS = ("static", "test", "ops", "e2e")


@pytest.fixture(scope="module")
def workflow() -> dict[str | bool, Any]:
    loaded: dict[str | bool, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    return loaded


@pytest.fixture(scope="module")
def jobs(workflow: dict[str | bool, Any]) -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = workflow["jobs"]
    return loaded


def test_pull_request_trigger_and_concurrency_stay(workflow: dict[str | bool, Any]) -> None:
    # PyYAML reads the bare key `on` as the boolean True.
    assert "pull_request" in workflow[True]
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }


def test_the_workflow_has_only_the_check_jobs_and_the_gate(
    jobs: dict[str, dict[str, Any]],
) -> None:
    assert set(jobs) == {"changes", *CHECK_JOBS, "ci"}


@pytest.mark.parametrize("job", CHECK_JOBS)
def test_check_jobs_start_together_after_changes(jobs: dict[str, dict[str, Any]], job: str) -> None:
    assert jobs[job]["needs"] == "changes"


@pytest.mark.parametrize("job", CHECK_JOBS)
def test_each_check_job_is_gated_by_its_changes_output(
    jobs: dict[str, dict[str, Any]], job: str
) -> None:
    """A job-level `if:`, so a skipped job reports Success and never blocks the merge."""
    assert jobs[job]["if"] == f"needs.changes.outputs.{job} == 'true'"


def test_changes_exposes_one_output_per_check_job(jobs: dict[str, dict[str, Any]]) -> None:
    assert jobs["changes"]["outputs"] == {
        job: f"${{{{ steps.flags.outputs.{job} }}}}" for job in CHECK_JOBS
    }


def test_changes_passes_base_on_pull_request_and_all_on_push(
    jobs: dict[str, dict[str, Any]],
) -> None:
    [step] = [s for s in jobs["changes"]["steps"] if s.get("id") == "flags"]
    run = step["run"]
    assert 'if [ "$GITHUB_EVENT_NAME" = pull_request ]; then' in run
    assert 'scripts/ci_changes.py --base "origin/$GITHUB_BASE_REF" >> "$GITHUB_OUTPUT"' in run
    assert 'scripts/ci_changes.py --all >> "$GITHUB_OUTPUT"' in run
    # The base ref comes from the runner's environment, never interpolated into the script.
    assert "${{" not in run


def test_changes_fetches_the_history_it_diffs(jobs: dict[str, dict[str, Any]]) -> None:
    [checkout] = [
        s for s in jobs["changes"]["steps"] if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkout["with"]["fetch-depth"] == 0


def test_no_workflow_level_paths_filter(workflow: dict[str | bool, Any]) -> None:
    """GitHub leaves a path-filtered workflow's required check Pending, which blocks the merge."""
    for trigger in (workflow[True] or {}).values():
        assert not {"paths", "paths-ignore"} & set(trigger or {})


def test_push_runs_on_main_only(workflow: dict[str | bool, Any]) -> None:
    assert workflow[True]["push"] == {"branches": ["main"]}


def test_ci_gate_needs_every_job_and_always_runs(jobs: dict[str, dict[str, Any]]) -> None:
    """`changes` is in `needs` too: if it fails, the skipped check jobs can't pass the gate."""
    gate = jobs["ci"]
    assert sorted(gate["needs"]) == sorted(["changes", *CHECK_JOBS])
    assert gate["if"] == "always()"


def test_ci_gate_fails_when_a_job_failed_or_was_cancelled(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """A skipped job passes the gate, so a job skipped by a path rule never blocks a merge."""
    [step] = jobs["ci"]["steps"]
    assert step["if"] == (
        "contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled')"
    )
    assert step["run"] == "exit 1"


@pytest.mark.parametrize("job", ["changes", *CHECK_JOBS])
def test_every_check_job_installs_the_pinned_uv(jobs: dict[str, dict[str, Any]], job: str) -> None:
    steps = [s for s in jobs[job]["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert len(steps) == 1
    assert steps[0]["with"]["version"]


@pytest.mark.parametrize("job", ["static", "test"])
def test_jobs_that_diff_against_main_fetch_the_full_history(
    jobs: dict[str, dict[str, Any]], job: str
) -> None:
    """squawk (`static`) and the upgrade-with-data test (`test`) diff against origin/main."""
    [checkout] = [
        s for s in jobs[job]["steps"] if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkout["with"]["fetch-depth"] == 0


def test_only_the_test_job_runs_postgres(jobs: dict[str, dict[str, Any]]) -> None:
    assert [name for name, job in jobs.items() if "services" in job] == ["test"]


@pytest.mark.parametrize(
    ("job", "command"),
    [
        ("static", "uv run ruff check ."),
        ("static", "uv run ruff format --check ."),
        ("static", "uv run mypy"),
        ("static", "uv run lint-imports"),
        ("static", "uv run python scripts/squawk_migrations.py --base origin/main"),
        ("static", "uv run python scripts/check_uv_pins.py"),
        ("static", "uv run python scripts/generate_openapi.py"),
        ("test", "uv run alembic upgrade head"),
        ("test", "uv run pytest tests/unit tests/integration tests/migrations"),
        ("ops", "make cloud-init-check"),
        ("ops", "make alloy-check"),
        ("ops", "uv run pytest tests/ops"),
        ("e2e", "make e2e"),
    ],
)
def test_each_check_runs_in_its_job(
    jobs: dict[str, dict[str, Any]], job: str, command: str
) -> None:
    runs = "\n".join(step.get("run", "") for step in jobs[job]["steps"])
    assert command in runs


def test_the_test_and_ops_jobs_together_run_every_testpath_once(
    jobs: dict[str, dict[str, Any]],
) -> None:
    """A folder added to `testpaths` fails here until a job runs it, and none runs twice."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    testpaths = pyproject["tool"]["pytest"]["ini_options"]["testpaths"]
    ran = [
        path
        for job in ("test", "ops")
        for step in jobs[job]["steps"]
        if step.get("run", "").startswith("uv run pytest ")
        for path in shlex.split(step["run"])[3:]
    ]
    assert sorted(ran) == sorted(testpaths)
