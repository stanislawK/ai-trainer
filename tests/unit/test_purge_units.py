"""The daily conversation purge's systemd units (ticket #226, ADR-0021 "Retention", ADR-0023 "The
server's scheduled jobs"): a oneshot service as `deploy` that runs `ai-trainer purge-conversations`
inside the production app container, and a timer that fires before the nightly backup."""

import configparser
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "deploy/systemd"
APP_CONTAINER = "ai-trainer-prod-app-1"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string((UNITS / name).read_text())
    return parser


@pytest.fixture(scope="module")
def service() -> configparser.ConfigParser:
    return _unit("ai-trainer-purge.service")


@pytest.fixture(scope="module")
def timer() -> configparser.ConfigParser:
    return _unit("ai-trainer-purge.timer")


def test_service_runs_the_purge_cli_in_the_app_container_as_deploy(
    service: configparser.ConfigParser,
) -> None:
    unit = service["Service"]
    assert unit["Type"] == "oneshot"
    assert unit["User"] == "deploy"
    assert unit["Group"] == "deploy"
    # `docker exec` fails when the container is stopped or missing, so the unit fails with it.
    assert unit["ExecStart"] == (
        f"/usr/bin/docker exec {APP_CONTAINER} ai-trainer purge-conversations"
    )


def test_service_targets_the_prod_compose_app_container() -> None:
    compose = yaml.safe_load((ROOT / "compose.prod.yaml").read_text())
    assert f"{compose['name']}-app-1" == APP_CONTAINER
    assert "app" in compose["services"]


def test_service_fails_instead_of_hanging(service: configparser.ConfigParser) -> None:
    # A oneshot has no start timeout by default: a stuck run would stay `activating`, never show
    # in `systemctl --failed`, and swallow the next days' runs.
    assert service["Service"]["TimeoutStartSec"] == "15min"


def test_service_logs_under_its_own_identifier(service: configparser.ConfigParser) -> None:
    assert service["Service"]["SyslogIdentifier"] == "ai-trainer-purge"


def test_service_is_hardened(service: configparser.ConfigParser) -> None:
    unit = service["Service"]
    assert unit["NoNewPrivileges"] == "yes"
    assert unit["PrivateTmp"] == "yes"
    assert unit["UMask"] == "0077"


def test_service_waits_for_docker(service: configparser.ConfigParser) -> None:
    unit = service["Unit"]
    assert "docker.service" in unit["After"].split()
    assert "docker.service" in unit["Wants"].split()


def test_timer_fires_daily_before_the_backup_and_catches_up(
    timer: configparser.ConfigParser,
) -> None:
    unit = timer["Timer"]
    assert unit["OnCalendar"] == "*-*-* 02:00:00 UTC"
    assert unit["Persistent"] == "true"
    assert unit["Unit"] == "ai-trainer-purge.service"
    assert timer["Install"]["WantedBy"] == "timers.target"
    # Even at its latest, the purge starts before the 02:30 backup, so that night's dump already
    # leaves out the expired conversations.
    assert unit["RandomizedDelaySec"] == "15m"
    assert _unit("ai-trainer-backup.timer")["Timer"]["OnCalendar"] == "*-*-* 02:30:00 UTC"
