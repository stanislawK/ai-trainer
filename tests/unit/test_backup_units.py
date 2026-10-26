"""The nightly backup's systemd units (ticket #223, ADR-0023 "The server's scheduled jobs"): a
oneshot service as `deploy` reading its R2 credentials from a 0600 env file, and a timer that
catches up on a missed night."""

import configparser
import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "deploy/systemd"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string((UNITS / name).read_text())
    return parser


@pytest.fixture(scope="module")
def service() -> configparser.ConfigParser:
    return _unit("ai-trainer-backup.service")


@pytest.fixture(scope="module")
def timer() -> configparser.ConfigParser:
    return _unit("ai-trainer-backup.timer")


def test_service_runs_the_nightly_backup_once_as_deploy(
    service: configparser.ConfigParser,
) -> None:
    unit = service["Service"]
    assert unit["Type"] == "oneshot"
    assert unit["User"] == "deploy"
    assert unit["Group"] == "deploy"
    assert unit["ExecStart"] == "/opt/ai-trainer/deploy/backup.sh nightly"


def test_service_reads_credentials_from_the_servers_env_file(
    service: configparser.ConfigParser,
) -> None:
    assert service["Service"]["EnvironmentFile"] == "/opt/ai-trainer/backup.env"


def test_service_is_hardened(service: configparser.ConfigParser) -> None:
    unit = service["Service"]
    assert unit["NoNewPrivileges"] == "yes"
    assert unit["PrivateTmp"] == "yes"
    assert unit["UMask"] == "0077"
    # A oneshot has no start timeout by default, so a hung rclone stays activating and
    # systemd skips later nights.
    assert unit["TimeoutStartSec"] == "30min"
    # SIGTERM would close the dump pipe while rclone is still running.
    assert unit["KillSignal"] == "SIGKILL"


def test_service_waits_for_docker_and_the_network(service: configparser.ConfigParser) -> None:
    unit = service["Unit"]
    for dependency in ("docker.service", "network-online.target"):
        assert dependency in unit["After"].split()
        assert dependency in unit["Wants"].split()


def test_timer_fires_nightly_and_catches_up_after_downtime(
    timer: configparser.ConfigParser,
) -> None:
    unit = timer["Timer"]
    assert unit["OnCalendar"] == "*-*-* 02:30:00 UTC"
    assert unit["Persistent"] == "true"
    assert unit["Unit"] == "ai-trainer-backup.service"
    assert timer["Install"]["WantedBy"] == "timers.target"


def test_backup_env_example_lists_every_setting_with_no_real_value() -> None:
    lines = (ROOT / "deploy/backup.env.example").read_text().splitlines()
    settings = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    assert sorted(settings) == sorted(
        [
            "BACKUP_DEST",
            "BACKUP_AGE_RECIPIENT",
            "RCLONE_CONFIG_R2_TYPE",
            "RCLONE_CONFIG_R2_PROVIDER",
            "RCLONE_CONFIG_R2_ENDPOINT",
            "RCLONE_CONFIG_R2_ACCESS_KEY_ID",
            "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY",
            "RCLONE_CONFIG_R2_NO_CHECK_BUCKET",
        ]
    )
    assert settings["RCLONE_CONFIG_R2_TYPE"] == "s3"
    assert settings["RCLONE_CONFIG_R2_PROVIDER"] == "Cloudflare"
    # The token can write objects but not create buckets, so rclone mustn't try.
    assert settings["RCLONE_CONFIG_R2_NO_CHECK_BUCKET"] == "true"
    assert settings["RCLONE_CONFIG_R2_ACCESS_KEY_ID"] == ""
    assert settings["RCLONE_CONFIG_R2_SECRET_ACCESS_KEY"] == ""


def test_service_logs_under_the_identifier_alloy_keeps(service: configparser.ConfigParser) -> None:
    # Without it the journal labels the lines `backup.sh` and Alloy ships none (ADR-0030, #274).
    assert service["Service"]["SyslogIdentifier"] == "ai-trainer-backup"


@pytest.mark.parametrize(
    ("result", "printed"),
    [
        ("timeout", "backup: result=failed reason=killed\n"),
        ("signal", "backup: result=failed reason=killed\n"),
        # backup.sh printed its own line in these; a second would make two.
        ("success", ""),
        ("exit-code", ""),
    ],
)
def test_a_killed_run_gets_its_failed_outcome_line_from_the_unit(
    service: configparser.ConfigParser, result: str, printed: str
) -> None:
    """KillSignal=SIGKILL leaves backup.sh no chance to print one (ADR-0030, #274)."""
    command = service["Service"]["ExecStopPost"].replace("$$", "$")

    run = subprocess.run(
        shlex.split(command),
        env={"SERVICE_RESULT": result, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )

    assert run.stdout == printed
