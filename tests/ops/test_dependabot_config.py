"""Dependabot keeps the Docker `python` image on the minor ADR-0002 locks (#484)."""

from pathlib import Path
from typing import Any

import yaml

DEPENDABOT = Path(__file__).resolve().parent.parent.parent / ".github" / "dependabot.yml"


def _docker_update() -> dict[str, Any]:
    config = yaml.safe_load(DEPENDABOT.read_text())
    (docker,) = [u for u in config["updates"] if u["package-ecosystem"] == "docker"]
    return dict(docker)


def test_docker_ignores_python_minor_and_major_bumps() -> None:
    ignores = {rule["dependency-name"]: rule for rule in _docker_update().get("ignore", [])}

    assert set(ignores["python"]["update-types"]) == {
        "version-update:semver-minor",
        "version-update:semver-major",
    }


def test_the_python_ignore_cites_adr_0002() -> None:
    text = DEPENDABOT.read_text()

    assert (
        "ADR-0002"
        in text.split("dependency-name: python")[0].rsplit("package-ecosystem: docker", 1)[1]
    )
