"""`deploy/monitor-password.sh` sets the Postgres monitor role's password from alloy.env (ADR-0030,
ticket #273). The migration creates the role without one, since no secret is in the repo.

The script runs against a stand-in `docker` on PATH that records its arguments and the SQL it
is given on stdin. The real run is `make smoke-up` and deploy.sh, against the `db` container."""

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/monitor-password.sh"
PASSWORD = "0123456789abcdef0123456789abcdef"
TOKEN = "glc_do-not-read-this-token"

FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
stdin = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "stdin": stdin}) + "\\n")
if os.environ.get("FAKE_PSQL_FAIL"):
    sys.stderr.write('ERROR:  role "ai_trainer_monitor" does not exist\\n')
    sys.exit(3)
"""


def _env_file(tmp_path: Path, password: str | None = PASSWORD) -> Path:
    lines = [
        "# Grafana Cloud",
        "GRAFANA_CLOUD_METRICS_URL=https://prometheus.example/api/prom/push",
        f"GRAFANA_CLOUD_TOKEN={TOKEN}",
    ]
    if password is not None:
        lines.append(f"POSTGRES_MONITOR_PASSWORD={password}")
    path = tmp_path / "alloy.env"
    path.write_text("\n".join(lines) + "\n")
    return path


def _run(
    tmp_path: Path, env_file: Path, *, fail: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.jsonl"
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(log)}
    if fail:
        env["FAKE_PSQL_FAIL"] = "1"
    result = subprocess.run(
        [str(SCRIPT), str(env_file), "docker", "compose", "-f", "compose.prod.yaml"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def test_sets_the_password_through_psql_in_the_db_container(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, _env_file(tmp_path))

    assert result.returncode == 0, result.stderr
    (call,) = calls
    assert call["args"][:3] == ["compose", "-f", "compose.prod.yaml"]
    assert call["args"][3:7] == ["exec", "-T", "db", "psql"]
    assert {"-q", "ON_ERROR_STOP=1", "ai_trainer"} <= set(call["args"])
    assert f"ALTER ROLE ai_trainer_monitor PASSWORD '{PASSWORD}';" in call["stdin"]
    assert result.stdout.strip() == "monitor-password: result=ok role=ai_trainer_monitor"


def test_the_password_reaches_only_psqls_stdin(tmp_path: Path) -> None:
    """Never an argument (visible in `ps`) and never the output (the journal ships it)."""
    result, calls = _run(tmp_path, _env_file(tmp_path))

    (call,) = calls
    assert PASSWORD not in " ".join(call["args"])
    assert PASSWORD not in result.stdout + result.stderr


def test_a_failing_statement_is_never_logged_with_its_text(tmp_path: Path) -> None:
    """Postgres logs a failed statement's text by default, and Alloy ships the db's logs."""
    _, calls = _run(tmp_path, _env_file(tmp_path))

    (call,) = calls
    sql = call["stdin"]
    alter = sql.index("ALTER ROLE")
    for setting in (
        "SET log_min_error_statement = panic;",
        "SET log_statement = none;",
        "SET log_min_duration_statement = -1;",
    ):
        assert -1 < sql.index(setting) < alter, setting


def test_reads_only_the_monitor_password(tmp_path: Path) -> None:
    _, calls = _run(tmp_path, _env_file(tmp_path))

    (call,) = calls
    assert TOKEN not in call["stdin"]
    assert "GRAFANA" not in call["stdin"]


def test_a_failing_psql_fails_without_the_password(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, _env_file(tmp_path), fail=True)

    assert result.returncode != 0
    assert result.stderr.strip().endswith("monitor-password: result=failed role=ai_trainer_monitor")
    assert PASSWORD not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "password",
    [
        None,
        "",
        "short1234",
        "has a space in it 0123456789",
        "quote'0123456789abcdef0123456789",
        "url@reserved/0123456789abcdef01",
        "$(touch pwned)0123456789abcdef01",
    ],
)
def test_a_missing_or_unsafe_password_sets_nothing(tmp_path: Path, password: str | None) -> None:
    """Letters and digits only, at least 24: safe in SQL and in the exporter's DSN as is."""
    result, calls = _run(tmp_path, _env_file(tmp_path, password))

    assert result.returncode == 2
    assert calls == []
    assert "POSTGRES_MONITOR_PASSWORD" in result.stderr
    if password:
        assert password not in result.stderr
    assert not (tmp_path / "pwned").exists()


def test_a_missing_env_file_sets_nothing(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, tmp_path / "absent.env")

    assert result.returncode == 2
    assert calls == []
    assert "absent.env" in result.stderr


def test_the_last_value_wins_as_compose_reads_it(tmp_path: Path) -> None:
    path = _env_file(tmp_path, "a" * 24)
    with path.open("a") as f:
        f.write(f"POSTGRES_MONITOR_PASSWORD={PASSWORD}\n")

    _, calls = _run(tmp_path, path)

    (call,) = calls
    assert f"PASSWORD '{PASSWORD}'" in call["stdin"]
