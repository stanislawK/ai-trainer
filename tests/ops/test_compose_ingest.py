"""The `ingest` service in `compose.yaml` and the `make start` overlay (ADR-0036 invariant 6,
#462): an upload reaches `ingested` with no command typed, and a killed or OOM-killed worker
restarts on its own while the app keeps serving."""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / name).read_text())
    return loaded


def _env_example() -> dict[str, str]:
    lines = (ROOT / ".env.example").read_text().splitlines()
    pairs = [line.split("=", 1) for line in lines if line and not line.startswith("#")]
    return {key: value for key, value in pairs}


def test_dev_stack_runs_the_ingest_worker_with_storage() -> None:
    base = _load("compose.yaml")["services"]
    dev = _load("compose.dev.yaml")["services"]
    ingest, overlay = base["ingest"], dev["ingest"]

    assert ingest["build"] == base["app"]["build"]
    assert ingest["command"] == ["ai-trainer", "ingest-worker"]
    assert ingest["env_file"] == base["app"]["env_file"]
    assert ingest["environment"]["DATABASE_URL"] == base["app"]["environment"]["DATABASE_URL"]
    # The same local bucket the upload page writes to.
    assert overlay["environment"] == dev["app"]["environment"]
    env = _env_example()
    for key in ("OBJECT_STORAGE_ACCESS_KEY_ID", "OBJECT_STORAGE_UPLOADS_BUCKET"):
        assert overlay["environment"][key] == env[key], key
    assert overlay["depends_on"] == {
        "migrate": {"condition": "service_completed_successfully"},
        "storage": {"condition": "service_started"},
    }
    # The worker has no reload: a code change restarts it.
    watch = overlay["develop"]["watch"]
    assert {"action": "sync+restart", "path": "./src", "target": "/app/src"}.items() <= watch[
        0
    ].items()
    assert {"action": "rebuild", "path": "./uv.lock"} in watch


def test_ingest_restarts_on_failure_and_shares_nothing_with_app() -> None:
    base = _load("compose.yaml")["services"]
    ingest = base["ingest"]

    assert ingest["restart"] == "on-failure"
    assert ingest["depends_on"] == {"db": {"condition": "service_healthy"}}
    # The image's HEALTHCHECK probes :8000, which the worker doesn't serve.
    assert ingest["healthcheck"] == {"disable": True}
    assert "ports" not in ingest
    for name in ("app", "db"):
        assert "ingest" not in base[name].get("depends_on", {}), name
    for name, svc in _load("compose.dev.yaml")["services"].items():
        assert "ingest" not in svc.get("depends_on", {}), name


def test_ingest_has_limits_the_acceptance_gate_can_lower() -> None:
    """`INGEST_MEMORY_LIMIT=64m make start` forces an OOM kill of this container alone. It isn't
    a Settings key, so it stays out of `.env.example` (tests/unit/test_settings.py)."""
    limits = _load("compose.yaml")["services"]["ingest"]["deploy"]["resources"]["limits"]

    assert limits == {"memory": "${INGEST_MEMORY_LIMIT:-768m}", "cpus": "1.0"}
    assert "INGEST_MEMORY_LIMIT" not in (ROOT / ".env.example").read_text()
