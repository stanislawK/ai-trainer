"""The deploy poller's systemd units (ticket #225, ADR-0023 "Pull-based deploy on the server"): a
oneshot service as `deploy` that gets the GitHub token as a systemd credential, and a timer that
starts it every two minutes."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / "deploy/systemd"


def _unit(name: str) -> dict[str, dict[str, list[str]]]:
    """Sections → keys → every value, since systemd allows a key more than once."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] = {}
    for line in (UNITS / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
        else:
            key, value = line.split("=", 1)
            current.setdefault(key, []).append(value)
    return sections


@pytest.fixture(scope="module")
def service() -> dict[str, dict[str, list[str]]]:
    return _unit("ai-trainer-deploy.service")


@pytest.fixture(scope="module")
def timer() -> dict[str, dict[str, list[str]]]:
    return _unit("ai-trainer-deploy.timer")


def test_service_runs_the_poller_once_as_deploy(service: dict[str, dict[str, list[str]]]) -> None:
    unit = service["Service"]
    assert unit["Type"] == ["oneshot"]
    assert unit["User"] == ["deploy"]
    assert unit["Group"] == ["deploy"]
    assert unit["ExecStart"] == ["/opt/ai-trainer/deploy/deploy.sh"]


def test_service_gets_the_token_as_a_credential_not_an_environment_variable(
    service: dict[str, dict[str, list[str]]],
) -> None:
    unit = service["Service"]
    assert unit["LoadCredential"] == ["github-token:/opt/ai-trainer/github-token"]
    # The env files hold the site host and backup.sh's R2 settings, never the GitHub token.
    assert unit["EnvironmentFile"] == ["/opt/ai-trainer/deploy.env", "/opt/ai-trainer/backup.env"]


def test_service_is_hardened_and_cannot_hang(service: dict[str, dict[str, list[str]]]) -> None:
    unit = service["Service"]
    assert unit["NoNewPrivileges"] == ["yes"]
    assert unit["PrivateTmp"] == ["yes"]
    assert unit["UMask"] == ["0077"]
    assert unit["TimeoutStartSec"] == ["30min"]
    assert unit["SyslogIdentifier"] == ["ai-trainer-deploy"]


def test_service_waits_for_docker_and_the_network(
    service: dict[str, dict[str, list[str]]],
) -> None:
    unit = service["Unit"]
    for dependency in ("docker.service", "network-online.target"):
        assert dependency in unit["After"][0].split()
        assert dependency in unit["Wants"][0].split()


def test_timer_fires_every_two_minutes(timer: dict[str, dict[str, list[str]]]) -> None:
    unit = timer["Timer"]
    assert unit["OnCalendar"] == ["*-*-* *:00/2:00"]
    # systemd's default accuracy is a minute; an approved release must run within five.
    assert unit["AccuracySec"] == ["10s"]
    assert unit["Unit"] == ["ai-trainer-deploy.service"]
    # A missed poll isn't worth catching up on: the next one is two minutes away.
    assert "Persistent" not in unit
    assert timer["Install"]["WantedBy"] == ["timers.target"]


def test_deploy_env_example_holds_only_the_site_host() -> None:
    lines = (ROOT / "deploy/deploy.env.example").read_text().splitlines()
    settings = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    assert settings == {"SITE_HOST": "trainer.example.com"}
