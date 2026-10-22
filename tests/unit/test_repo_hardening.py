"""Committed repository hardening: workflows, Dependabot and CODEOWNERS (ADR-0023)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GITHUB = ROOT / ".github"
WORKFLOWS = sorted((GITHUB / "workflows").glob("*.yml"))
FULL_SHA = re.compile(r"@[0-9a-f]{40}(\s|$)")


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def test_there_are_workflows_to_check() -> None:
    assert WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_run_block_interpolates_inputs(path: Path) -> None:
    for step in _steps(_load(path)):
        run = step.get("run", "")
        assert "inputs." not in run, f"{path.name}: {step.get('name')} interpolates inputs in run"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_sha(path: Path) -> None:
    for line in path.read_text().splitlines():
        stripped = line.strip().removeprefix("- ")
        if stripped.startswith("uses:") and not stripped.startswith("uses: ./"):
            assert FULL_SHA.search(stripped), f"{path.name}: {stripped}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_defaults_to_read_only_contents(path: Path) -> None:
    assert _load(path)["permissions"] == {"contents": "read"}


def test_dependabot_covers_uv_docker_and_github_actions() -> None:
    config = _load(GITHUB / "dependabot.yml")
    assert config["version"] == 2
    ecosystems = {update["package-ecosystem"] for update in config["updates"]}
    assert ecosystems == {"uv", "docker", "github-actions"}
    for update in config["updates"]:
        assert update["directory"] == "/"
        assert update["schedule"]["interval"]


def test_codeowners_assigns_everything_to_the_owner() -> None:
    lines = [
        line.split()
        for line in (GITHUB / "CODEOWNERS").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines == [["*", "@stanislawK"]]
