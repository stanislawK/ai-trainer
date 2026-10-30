"""`ci.yml` runs its checks as parallel jobs behind one `ci` gate (ticket #356, ADR-0013
"Parallel, path-aware CI"): `static`, `test`, `ops` and `e2e` start together, and `ci`, the check
`main`'s ruleset requires, fails when any of them failed or was cancelled."""

import re
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
    assert set(jobs) == {*CHECK_JOBS, "ci"}


@pytest.mark.parametrize("job", CHECK_JOBS)
def test_check_jobs_start_together_with_no_needs(jobs: dict[str, dict[str, Any]], job: str) -> None:
    assert "needs" not in jobs[job]


def test_ci_gate_needs_every_check_job_and_always_runs(jobs: dict[str, dict[str, Any]]) -> None:
    gate = jobs["ci"]
    assert sorted(gate["needs"]) == sorted(CHECK_JOBS)
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


@pytest.mark.parametrize("job", CHECK_JOBS)
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


# The e2e image build (#358, ADR-0013 "Caches"): Buildx builds the app image with Docker's GitHub
# Actions cache, compose starts it without building, and a check proves the image holds the
# commit's own `src/`.
RELEASE_ACTIONS = ("docker/setup-buildx-action@", "docker/build-push-action@")
SOURCE_CHECK = "Check the image holds this commit's src/"


def _e2e_index(jobs: dict[str, dict[str, Any]], match: str) -> int:
    [index] = [
        i
        for i, step in enumerate(jobs["e2e"]["steps"])
        if step.get("uses", "").startswith(match) or step.get("name") == match
    ]
    return index


def _uses_gha_cache(jobs: dict[str, dict[str, Any]]) -> bool:
    return any(
        "type=gha" in str(step.get("with", {}).get("cache-from", ""))
        for step in jobs["e2e"]["steps"]
    )


def _caches_bullet() -> str:
    adr = (ROOT / "docs/adr/0013-testing-and-quality.md").read_text()
    [bullet] = [line for line in adr.splitlines() if line.startswith("  - **Caches:**")]
    return bullet


@pytest.mark.parametrize("action", RELEASE_ACTIONS)
def test_e2e_uses_the_docker_actions_release_yml_pins(
    jobs: dict[str, dict[str, Any]], action: str
) -> None:
    """No new third-party action: the same SHAs ADR-0023's release build already trusts."""
    release = (ROOT / ".github/workflows/release.yml").read_text()
    step = jobs["e2e"]["steps"][_e2e_index(jobs, action)]
    assert step["uses"] in release


def test_e2e_builds_the_app_image_with_the_gha_cache(jobs: dict[str, dict[str, Any]]) -> None:
    build = jobs["e2e"]["steps"][_e2e_index(jobs, "docker/build-push-action@")]["with"]
    assert build["context"] == "."
    assert build["load"] is True
    assert build["push"] is False
    assert build["cache-from"] == "type=gha,scope=e2e"
    assert build["cache-to"] == "type=gha,scope=e2e,mode=max"
    assert _e2e_index(jobs, "docker/setup-buildx-action@") < _e2e_index(
        jobs, "docker/build-push-action@"
    )


def test_the_built_tag_is_the_image_compose_runs(jobs: dict[str, dict[str, Any]]) -> None:
    """Compose names a built service's image `<project>-<service>`; `--no-build` runs that tag."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert "image" not in compose["services"]["app"]
    build = jobs["e2e"]["steps"][_e2e_index(jobs, "docker/build-push-action@")]["with"]
    assert build["tags"] == f"{compose['name']}-app:latest"


def test_e2e_starts_compose_without_building(jobs: dict[str, dict[str, Any]]) -> None:
    [up] = [s["run"] for s in jobs["e2e"]["steps"] if "docker compose up" in s.get("run", "")]
    assert up == "docker compose up -d --no-build"
    assert _e2e_index(jobs, "docker/build-push-action@") < _e2e_index(
        jobs, "Start the compose stack"
    )


def test_e2e_checks_the_image_holds_the_commits_source(jobs: dict[str, dict[str, Any]]) -> None:
    check = jobs["e2e"]["steps"][_e2e_index(jobs, SOURCE_CHECK)]["run"]
    assert "ai-trainer-app:latest" in check
    assert "find src" in check
    assert (
        _e2e_index(jobs, "docker/build-push-action@")
        < _e2e_index(jobs, SOURCE_CHECK)
        < _e2e_index(jobs, "Start the compose stack")
    )


def test_playwright_browsers_are_not_cached(jobs: dict[str, dict[str, Any]]) -> None:
    """Playwright advises against it: restoring takes about as long as downloading."""
    steps = jobs["e2e"]["steps"]
    assert not [s for s in steps if s.get("uses", "").startswith("actions/cache")]
    assert "ms-playwright" not in yaml.safe_dump(steps)


def test_adr_0013_records_the_e2e_cache_outcome(jobs: dict[str, dict[str, Any]]) -> None:
    """Kept only on a measured saving of about 15 s or more; dropped otherwise, and said so."""
    bullet = _caches_bullet()
    if _uses_gha_cache(jobs):
        saving = re.search(r"saves about (\d+) s", bullet)
        assert saving, "the Caches bullet gives no measured saving"
        assert int(saving.group(1)) >= 15
        assert "kept" in bullet
    else:
        assert "dropped" in bullet
