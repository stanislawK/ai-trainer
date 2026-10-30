"""`deploy/compose.sh` runs Compose on the host with the variables compose.prod.yaml requires
(ticket #303). Compose interpolates the whole file, so `logs caddy` needs APP_IMAGE as much as
`up app` does; the wrapper reads it from app-image, as the poller records it (ADR-0023).

The script runs against a stand-in `docker` on PATH that writes its arguments and the variables
it was given to a JSON file, in a throwaway DEPLOY_ROOT."""

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/compose.sh"
IMAGE = "ghcr.io/stanislawk/ai-trainer@sha256:" + "a" * 64
OTHER_IMAGE = "ghcr.io/stanislawk/ai-trainer@sha256:" + "b" * 64
SITE_HOST = "trainer.example.com"

FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
keys = ("APP_IMAGE", "RELEASE_TAG", "SITE_HOST")
with open(os.environ["FAKE_CALL"], "w") as f:
    json.dump({"args": sys.argv[1:], "env": {k: os.environ.get(k) for k in keys}}, f)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


@pytest.fixture
def host(tmp_path: Path) -> Path:
    deploy_root = tmp_path / "opt"
    deploy_root.mkdir()
    (deploy_root / "deploy.env").write_text(f"SITE_HOST={SITE_HOST}\n")
    (deploy_root / "app-image").write_text(IMAGE + "\n")
    (deploy_root / "app-version").write_text("v0.1.0\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return deploy_root


def _run(
    host: Path, *args: str, **env: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    call = host.parent / "call.json"
    base = {
        "PATH": f"{host.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(host.parent),
        "DEPLOY_ROOT": str(host),
        "FAKE_CALL": str(call),
    }
    result = subprocess.run(
        [str(SCRIPT), *args],
        env=base | env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    recorded = json.loads(call.read_text()) if call.exists() else None
    return result, recorded


def test_runs_compose_on_the_prod_file_with_the_deployed_image(host: Path) -> None:
    result, call = _run(host, "logs", "caddy", "--since", "2h")
    assert result.returncode == 0, result.stderr
    assert call is not None
    assert call["args"] == [
        "compose",
        "--project-directory",
        str(host),
        "-f",
        str(host / "compose.prod.yaml"),
        "logs",
        "caddy",
        "--since",
        "2h",
    ]
    assert call["env"] == {"APP_IMAGE": IMAGE, "RELEASE_TAG": "v0.1.0", "SITE_HOST": SITE_HOST}


def test_an_image_given_by_hand_wins_over_app_image(host: Path) -> None:
    """A hand rollback names its digest: the wrapper never replaces it."""
    result, call = _run(host, "ps", APP_IMAGE=OTHER_IMAGE, RELEASE_TAG="v0.0.9")
    assert result.returncode == 0, result.stderr
    assert call is not None
    assert call["env"]["APP_IMAGE"] == OTHER_IMAGE
    assert call["env"]["RELEASE_TAG"] == "v0.0.9"


def test_without_app_version_the_release_tag_is_left_to_compose(host: Path) -> None:
    (host / "app-version").unlink()
    result, call = _run(host, "ps")
    assert result.returncode == 0, result.stderr
    assert call is not None
    assert call["env"]["RELEASE_TAG"] is None


def test_a_host_never_deployed_stops_before_compose(host: Path) -> None:
    (host / "app-image").unlink()
    result, call = _run(host, "ps")
    assert result.returncode == 2
    assert "no app-image" in result.stderr
    assert "APP_IMAGE" in result.stderr
    assert call is None


def test_an_empty_app_image_stops_before_compose(host: Path) -> None:
    (host / "app-image").write_text("\n")
    result, call = _run(host, "ps")
    assert result.returncode == 2
    assert "no app-image" in result.stderr
    assert call is None


def test_without_deploy_env_or_site_host_it_stops_before_compose(host: Path) -> None:
    (host / "deploy.env").unlink()
    result, call = _run(host, "ps")
    assert result.returncode == 2
    assert "SITE_HOST" in result.stderr
    assert call is None


def test_compose_exit_status_is_passed_on(host: Path) -> None:
    result, _call = _run(host, "ps", FAKE_EXIT="3")
    assert result.returncode == 3
