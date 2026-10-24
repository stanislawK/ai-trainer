"""`deploy/cloud-init.yaml` provisions a hardened Debian 13 host (ticket #222, ADR-0023 "Host"):
key-only SSH as `deploy`, no root login, unattended upgrades, Docker from Debian's archive, and a
`deploy`-owned `/opt/ai-trainer`. The schema check is `make cloud-init-check`; these tests pin
the security properties so a later edit can't quietly drop one."""

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CLOUD_INIT = ROOT / "deploy/cloud-init.yaml"
RUNBOOK = ROOT / "deploy/RUNBOOK.md"
# GitHub CLI's current apt signing key, as published in cli/cli's docs/install_linux.md. The
# older key, 2C6106201985B60E6C7AC87323F3D4EA75716059, expired on 2026-09-05.
GH_CLI_KEY = "7F38BBB59D064DBCB3D84D725612B36462313325"


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    loaded = yaml.safe_load(CLOUD_INIT.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _file(config: dict[str, Any], path: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = [f for f in config["write_files"] if f["path"] == path]
    assert len(matches) == 1, f"expected one write_files entry for {path}"
    return matches[0]


def _fingerprints(armored: str) -> list[str]:
    """The v4 fingerprint of each primary key in an armored public key block (RFC 4880 12.2):
    SHA-1 over 0x99, the two-byte body length, and the public-key packet's body."""
    lines = armored.strip().splitlines()
    body = lines[lines.index("") + 1 : -1]
    data = base64.b64decode("".join(line for line in body if not line.startswith("=")))
    fingerprints, offset = [], 0
    while offset < len(data):
        header = data[offset]
        assert header & 0x80, "not an OpenPGP packet"
        if header & 0x40:  # new format
            tag, first = header & 0x3F, data[offset + 1]
            if first < 192:
                length, start = first, offset + 2
            elif first < 224:
                length, start = ((first - 192) << 8) + data[offset + 2] + 192, offset + 3
            else:
                length, start = int.from_bytes(data[offset + 2 : offset + 6]), offset + 6
        else:  # old format
            tag, size = (header >> 2) & 0x0F, [1, 2, 4][header & 0x03]
            length = int.from_bytes(data[offset + 1 : offset + 1 + size])
            start = offset + 1 + size
        if tag == 6:
            packet = data[start : start + length]
            digest = hashlib.sha1(b"\x99" + len(packet).to_bytes(2) + packet).hexdigest()
            fingerprints.append(digest.upper())
        offset = start + length
    return fingerprints


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
    # GitHub CLI's is the only other apt source (#225).
    assert set(config["apt"]["sources"]) == {"github-cli"}


def test_gh_comes_from_github_clis_signed_apt_repository(config: dict[str, Any]) -> None:
    """Trixie's gh is 2.46, older than `gh attestation` (2.49), which deploy.sh needs (#225)."""
    assert "gh" in config["packages"]
    source = config["apt"]["sources"]["github-cli"]
    assert source["source"] == (
        "deb [signed-by=$KEY_FILE] https://cli.github.com/packages stable main"
    )
    assert _fingerprints(source["key"]) == [GH_CLI_KEY]


def test_gh_upgrades_are_applied_unattended(config: dict[str, Any]) -> None:
    """Unattended-upgrades only follows Debian's origins until told otherwise."""
    origins = _file(config, "/etc/apt/apt.conf.d/52ai-trainer-unattended-upgrades")
    assert origins["defer"] is True
    assert (
        'Unattended-Upgrade::Origins-Pattern {\n  "origin=gh,label=gh";\n};' in (origins["content"])
    )


def test_deploy_tools_are_installed(config: dict[str, Any]) -> None:
    # deploy.sh polls /health with curl and holds its lock with flock (util-linux).
    assert {"curl", "util-linux"} <= set(config["packages"])


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
