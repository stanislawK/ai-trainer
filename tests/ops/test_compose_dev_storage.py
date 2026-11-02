"""The local S3 service `make start` runs, so development needs no R2 account (ADR-0033, #420)."""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]


def _env_example() -> dict[str, str]:
    lines = (ROOT / ".env.example").read_text().splitlines()
    pairs = [line.split("=", 1) for line in lines if line and not line.startswith("#")]
    return {key: value for key, value in pairs}


def _compose() -> dict[str, Any]:
    compose: dict[str, Any] = yaml.safe_load((ROOT / "compose.dev.yaml").read_text())
    return compose


def _storage_command() -> str:
    return " ".join(_compose()["services"]["storage"]["entrypoint"])


def test_local_storage_matches_env_example() -> None:
    env = _env_example()
    app = _compose()["services"]["app"]["environment"]
    command = _storage_command()

    assert env["OBJECT_STORAGE_ENDPOINT"] == "http://localhost:7070"
    assert app["OBJECT_STORAGE_ENDPOINT"] == "http://storage:7070"
    for key in (
        "OBJECT_STORAGE_REGION",
        "OBJECT_STORAGE_ACCESS_KEY_ID",
        "OBJECT_STORAGE_SECRET_ACCESS_KEY",
        "OBJECT_STORAGE_UPLOADS_BUCKET",
    ):
        assert app[key] == env[key], key
    assert f"--access {env['OBJECT_STORAGE_ACCESS_KEY_ID']}" in command
    assert f"--secret {env['OBJECT_STORAGE_SECRET_ACCESS_KEY']}" in command
    assert f"--region {env['OBJECT_STORAGE_REGION']}" in command
    assert f"/data/{env['OBJECT_STORAGE_UPLOADS_BUCKET']}" in command


def test_local_storage_is_pinned_and_published_only_on_this_machine() -> None:
    storage = _compose()["services"]["storage"]

    assert "@sha256:" in storage["image"]
    assert storage["ports"] == ["127.0.0.1:7070:7070"]


def test_the_app_waits_for_local_storage_only_in_the_dev_overlay() -> None:
    assert "storage" in _compose()["services"]["app"]["depends_on"]
    base = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert "storage" not in base["services"]
    prod = yaml.safe_load((ROOT / "compose.prod.yaml").read_text())
    assert "storage" not in prod["services"]
