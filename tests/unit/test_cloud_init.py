"""`deploy/cloud-init.yaml` provisions a hardened Debian 13 host (ticket #222, ADR-0023 "Host"):
key-only SSH as `deploy`, no root login, unattended upgrades, Docker from Debian's archive, and a
`deploy`-owned `/opt/ai-trainer`. The schema check is `make cloud-init-check`; these tests pin
the security properties so a later edit can't quietly drop one."""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CLOUD_INIT = ROOT / "deploy/cloud-init.yaml"
RUNBOOK = ROOT / "deploy/RUNBOOK.md"


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    loaded = yaml.safe_load(CLOUD_INIT.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _file(config: dict[str, Any], path: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = [f for f in config["write_files"] if f["path"] == path]
    assert len(matches) == 1, f"expected one write_files entry for {path}"
    return matches[0]


def _sshd_options(config: dict[str, Any]) -> dict[str, str]:
    content: str = _file(config, "/etc/ssh/sshd_config.d/10-ai-trainer.conf")["content"]
    pairs = (line.split(None, 1) for line in content.splitlines() if line and line[0] != "#")
    return {key: value for key, value in pairs}


def test_is_cloud_config() -> None:
    assert CLOUD_INIT.read_text().startswith("#cloud-config\n")


def test_deploy_is_the_default_user_so_the_hetzner_key_lands_on_it(
    config: dict[str, Any],
) -> None:
    # The `user` dict overrides the distro's default user; the datasource's public keys (the
    # SSH key picked in Hetzner's console) go to the default user.
    assert config["users"] == ["default"]
    user = config["user"]
    assert user["name"] == "deploy"
    assert user["lock_passwd"] is True
    assert user["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
    assert "sudo" in config["packages"]
    assert "passwd" not in user
    assert "hashed_passwd" not in user
    assert "plain_text_passwd" not in user


def test_root_and_passwords_are_refused_by_cloud_init(config: dict[str, Any]) -> None:
    assert config["disable_root"] is True
    assert config["ssh_pwauth"] is False


def test_sshd_drop_in_allows_only_key_login_as_deploy(config: dict[str, Any]) -> None:
    assert _sshd_options(config) == {
        "PermitRootLogin": "no",
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "AuthenticationMethods": "publickey",
        "AllowUsers": "deploy",
    }


def test_sshd_drop_in_sorts_before_cloud_inits_own(config: dict[str, Any]) -> None:
    # sshd keeps the first value it reads; cloud-init writes 50-cloud-init.conf.
    name = Path(_file(config, "/etc/ssh/sshd_config.d/10-ai-trainer.conf")["path"]).name
    assert name < "50-cloud-init.conf"


def test_unattended_upgrades_is_installed_and_enabled(config: dict[str, Any]) -> None:
    assert "unattended-upgrades" in config["packages"]
    auto = _file(config, "/etc/apt/apt.conf.d/20auto-upgrades")
    assert auto["defer"] is True
    assert 'APT::Periodic::Update-Package-Lists "1";' in auto["content"]
    assert 'APT::Periodic::Unattended-Upgrade "1";' in auto["content"]


def test_docker_comes_from_debians_archive(config: dict[str, Any]) -> None:
    # Debian-Security patches these, and unattended-upgrades applies them; Docker's own apt
    # repository would need its origin added to unattended-upgrades by hand.
    assert {"docker.io", "docker-cli", "docker-compose"} <= set(config["packages"])
    assert "apt" not in config or "sources" not in config["apt"]


def test_backup_tools_come_from_debians_archive(config: dict[str, Any]) -> None:
    # deploy/backup.sh encrypts with age and uploads with rclone (#223).
    assert {"age", "rclone"} <= set(config["packages"])


def test_deploy_joins_docker_after_the_packages_create_the_group(
    config: dict[str, Any],
) -> None:
    assert "docker" not in config["user"].get("groups", [])
    assert ["usermod", "-aG", "docker", "deploy"] in config["runcmd"]


def test_opt_ai_trainer_belongs_to_deploy_and_certs_to_root(config: dict[str, Any]) -> None:
    runcmd = config["runcmd"]
    assert ["install", "-d", "-o", "deploy", "-g", "deploy", "-m", "0750", "/opt/ai-trainer"] in (
        runcmd
    )
    assert [
        "install",
        "-d",
        "-o",
        "root",
        "-g",
        "root",
        "-m",
        "0700",
        "/opt/ai-trainer/certs",
    ] in runcmd


def test_upgrades_and_reboots_once_provisioned(config: dict[str, Any]) -> None:
    assert config["package_update"] is True
    assert config["package_upgrade"] is True
    assert config["power_state"]["mode"] == "reboot"


def test_runbook_records_type_and_price_privately_never_in_the_repo() -> None:
    # The repo is public; the owner keeps the record outside it.
    text = RUNBOOK.read_text()
    assert "## Provisioning record" in text
    record = text.split("## Provisioning record", 1)[1]
    assert "outside this repo" in record
    assert "Never commit the values" in record
    for field in ("Server type", "Price at order time", "Location", "Ordered on"):
        assert field in record
    assert "|" not in record, "no table to fill in and commit"
