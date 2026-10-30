"""`release.yml` publishes an attested image on a `v*` tag (ticket #219, ADR-0023 "Release
pipeline"): it runs only on such a tag, pushes by digest, guards against secrets before it attests,
and widens permissions per job only. Its `release` job waits for the owner's approval of the
`production` environment, then creates the deployment the server polls for (#220)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/stanislawk/ai-trainer"


@pytest.fixture(scope="module")
def workflow() -> dict[str | bool, Any]:
    loaded: dict[str | bool, Any] = yaml.safe_load(
        (ROOT / ".github/workflows/release.yml").read_text()
    )
    return loaded


@pytest.fixture(scope="module")
def build(workflow: dict[str | bool, Any]) -> dict[str, Any]:
    job: dict[str, Any] = workflow["jobs"]["build"]
    return job


@pytest.fixture(scope="module")
def release(workflow: dict[str | bool, Any]) -> dict[str, Any]:
    job: dict[str, Any] = workflow["jobs"]["release"]
    return job


@pytest.fixture(scope="module")
def release_script(release: dict[str, Any]) -> str:
    return "\n".join(step["run"] for step in release["steps"])


def _step(job: dict[str, Any], action: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = [
        step for step in job["steps"] if step.get("uses", "").startswith(f"{action}@")
    ]
    assert len(matches) == 1, f"expected one {action} step"
    return matches[0]


def _index(job: dict[str, Any], step: dict[str, Any]) -> int:
    steps: list[dict[str, Any]] = job["steps"]
    return steps.index(step)


def test_runs_only_on_a_v_tag_push(workflow: dict[str | bool, Any]) -> None:
    # PyYAML reads the bare key `on` as the boolean True.
    assert workflow[True] == {"push": {"tags": ["v*"]}}


def test_build_widens_permissions_only_as_far_as_publishing_and_attesting(
    build: dict[str, Any],
) -> None:
    assert build["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }


def test_checkout_keeps_no_token_on_disk(build: dict[str, Any]) -> None:
    assert _step(build, "actions/checkout")["with"]["persist-credentials"] is False


def test_logs_in_to_ghcr_with_the_job_token(build: dict[str, Any]) -> None:
    login = _step(build, "docker/login-action")["with"]
    assert login["registry"] == "ghcr.io"
    assert login["password"] == "${{ secrets.GITHUB_TOKEN }}"


def test_pushes_the_image_tagged_with_the_version(build: dict[str, Any]) -> None:
    push = _step(build, "docker/build-push-action")["with"]
    assert push["push"] is True
    assert push["tags"] == f"{IMAGE}:${{{{ github.ref_name }}}}"
    assert push["platforms"] == "linux/amd64"


def test_labels_the_image_with_its_source_repo(build: dict[str, Any]) -> None:
    """GHCR links the package to this repository through this label."""
    labels = _step(build, "docker/build-push-action")["with"]["labels"]
    assert "org.opencontainers.image.source=https://github.com/stanislawK/ai-trainer" in labels


def test_build_gets_no_secrets_or_build_args(build: dict[str, Any]) -> None:
    push = _step(build, "docker/build-push-action")["with"]
    for key in ("secrets", "secret-envs", "secret-files", "build-args"):
        assert key not in push


def test_digest_is_a_job_output(build: dict[str, Any]) -> None:
    push = _step(build, "docker/build-push-action")
    assert build["outputs"]["digest"] == f"${{{{ steps.{push['id']}.outputs.digest }}}}"


def test_attests_the_pushed_digest_into_the_registry(build: dict[str, Any]) -> None:
    push = _step(build, "docker/build-push-action")
    attest = _step(build, "actions/attest-build-provenance")["with"]
    assert attest["subject-name"] == IMAGE
    assert attest["subject-digest"] == f"${{{{ steps.{push['id']}.outputs.digest }}}}"
    assert attest["push-to-registry"] is True
    # Storage records exist only for organization-owned repositories and need an extra
    # `artifact-metadata: write` permission; this repository is user-owned.
    assert attest["create-storage-record"] is False


def test_secret_guard_runs_on_the_pushed_digest_before_attesting(build: dict[str, Any]) -> None:
    """An image that fails the guard is never attested, so the server never runs it."""
    push = _step(build, "docker/build-push-action")
    attest = _step(build, "actions/attest-build-provenance")
    [guard] = [step for step in build["steps"] if step.get("id") == "secret-guard"]
    assert _index(build, push) < _index(build, guard) < _index(build, attest)
    assert guard["env"]["DIGEST"] == f"${{{{ steps.{push['id']}.outputs.digest }}}}"
    assert '"$IMAGE@$DIGEST"' in guard["run"]


@pytest.mark.parametrize("pattern", ["'.env*'", "'*.env'", "'*key.pem'", "'*.key'"])
def test_secret_guard_looks_for_env_files_and_private_keys(
    build: dict[str, Any], pattern: str
) -> None:
    [guard] = [step for step in build["steps"] if step.get("id") == "secret-guard"]
    assert f"-name {pattern}" in guard["run"]


@pytest.mark.parametrize(
    "pattern",
    # At any depth, not only the root: `make smoke-setup` writes `.smoke/.env`, `.smoke/db.env`
    # and test private keys into the working tree (#221).
    [
        "**/.env",
        "**/.env.*",
        "**/*.env",
        "**/*.pem",
        "**/*.key",
        ".smoke",
        ".acceptance",
        ".playwright-mcp",
    ],
)
def test_dockerignore_keeps_secrets_and_local_state_out_of_the_build_context(
    pattern: str,
) -> None:
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    assert pattern in lines


def test_ci_runs_on_main_pushes_only_so_a_tag_starts_nothing_but_a_release() -> None:
    """A pull request runs CI once, on `pull_request`, and `main` again after the merge (#356)."""
    ci: dict[str | bool, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert ci[True]["push"] == {"branches": ["main"]}


def test_release_waits_for_the_build_and_the_production_environment(
    release: dict[str, Any],
) -> None:
    """The environment's required reviewer (the owner) holds the job until they approve."""
    assert release["needs"] == "build"
    assert release["environment"]["name"] == "production"


def test_release_environment_creates_no_deployment_of_its_own(release: dict[str, Any]) -> None:
    """Without `deployment: false` GitHub creates a `production` deployment while the job waits,
    and leaves it behind on a rejection. The only deployment is the one the approved job posts."""
    assert release["environment"]["deployment"] is False


def test_release_token_can_only_write_deployments(release: dict[str, Any]) -> None:
    assert release["permissions"] == {"deployments": "write"}


def test_release_runs_only_shell_steps_with_no_checkout(release: dict[str, Any]) -> None:
    for step in release["steps"]:
        assert "uses" not in step
        assert "run" in step


def test_release_reads_the_digest_and_token_through_env_never_inline(
    release: dict[str, Any], release_script: str
) -> None:
    envs = [step.get("env", {}) for step in release["steps"]]
    assert {"DIGEST": "${{ needs.build.outputs.digest }}"}.items() <= envs[0].items()
    assert all(env.get("GH_TOKEN") == "${{ github.token }}" for env in envs if "GH_TOKEN" in env)
    assert any("GH_TOKEN" in env for env in envs)
    assert "${{" not in release_script


def test_release_refuses_anything_but_a_sha256_digest(release_script: str) -> None:
    assert "^sha256:[0-9a-f]{64}$" in release_script
    assert release_script.index("sha256:[0-9a-f]{64}") < release_script.index("/deployments")


def test_release_creates_a_production_deployment_whose_payload_is_the_digest(
    release_script: str,
) -> None:
    assert '"repos/$GITHUB_REPOSITORY/deployments"' in release_script
    for field in (
        "ref: $ref",
        'environment: "production"',
        "production_environment: true",
        "payload: {digest: $digest}",
        "auto_merge: false",
        # The run's own checks are still in progress on the tagged commit; `main`'s ruleset
        # already required `ci` before the commit merged.
        "required_contexts: []",
    ):
        assert field in release_script


def test_release_marks_the_deployment_queued_for_the_poller(release_script: str) -> None:
    assert '"repos/$GITHUB_REPOSITORY/deployments/$id/statuses"' in release_script
    assert "state=queued" in release_script
    assert release_script.index('/deployments"') < release_script.index("/statuses")


def test_release_logs_ids_only_not_api_responses(release_script: str) -> None:
    """Each `gh api` call keeps only an id, so the log holds no response bodies."""
    joined = release_script.replace("\\\n", " ")
    calls = [line for line in joined.splitlines() if "gh api" in line]
    assert len(calls) == 2
    assert all("--jq .id" in call for call in calls)
