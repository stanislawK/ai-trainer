"""deploy/install.sh copies the host files the poller does not update (ADR-0023).

Secrets stay out of the script. These tests never open a connection: a missing or extra
argument exits before scp, and the file list is the array in the script.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/install.sh"

HOST_FILES = (
    "compose.prod.yaml",
    "deploy/Caddyfile",
    "deploy/alloy/config.alloy",
    "deploy/deploy.sh",
    "deploy/backup.sh",
    "deploy/monitor-password.sh",
    "deploy/compose.sh",
    "deploy/systemd/ai-trainer-deploy.service",
    "deploy/systemd/ai-trainer-deploy.timer",
    "deploy/systemd/ai-trainer-backup.service",
    "deploy/systemd/ai-trainer-backup.timer",
    "deploy/systemd/ai-trainer-purge.service",
    "deploy/systemd/ai-trainer-purge.timer",
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_usage_without_a_host_exits_before_any_copy() -> None:
    result = _run()
    assert result.returncode == 2
    assert "usage: deploy/install.sh" in result.stderr
    assert result.stdout == ""


def test_usage_with_extra_arguments_exits_before_any_copy() -> None:
    result = _run("203.0.113.10", "extra")
    assert result.returncode == 2
    assert "usage: deploy/install.sh" in result.stderr


def test_a_flag_is_not_accepted_as_a_host() -> None:
    result = _run("-h")
    assert result.returncode == 2
    assert "usage: deploy/install.sh" in result.stderr


def test_copies_only_the_host_files_the_poller_does_not_update() -> None:
    text = SCRIPT.read_text()
    match = re.search(r"files=\(\n(?P<body>.*?)\n\)", text, re.DOTALL)
    assert match is not None
    specs: list[tuple[str, str, str, str]] = []
    for line in match.group("body").splitlines():
        line = line.strip().strip("\"'")
        if not line or line.startswith("#"):
            continue
        source, dest, mode, owner = line.split("|")
        specs.append((source, dest, mode, owner))
    sources = tuple(spec[0] for spec in specs)
    assert sources == HOST_FILES
    outside = text[: match.start()] + text[match.end() :]
    for source, dest, mode, owner in specs:
        assert mode in {"0644", "0755"}
        assert owner in {"deploy", "root"}
        # The recreate command names the compose file it runs. Nothing else is listed twice.
        if dest != "/opt/ai-trainer/compose.prod.yaml":
            assert dest not in outside
        assert source not in outside or source == "compose.prod.yaml"
    assert "github-token" not in match.group("body")
    assert ".env" not in match.group("body")
    assert "certs/" not in match.group("body")
    assert "mktemp -d" in text
    assert "rm -rf" in text
    # The running ones among Caddy and Alloy, collected in `recreate`.
    assert '--force-recreate "\\${recreate[@]}"' in text
    # Compose interpolates the whole file, so recreating Caddy still needs APP_IMAGE.
    assert "export APP_IMAGE=" in text
    assert "/opt/ai-trainer/app-image" in text
    # Wait for the poller's lock. flock -n would skip the install instead of waiting.
    assert "flock /opt/ai-trainer/.deploy.lock" in text
    assert "flock -n" not in text


def test_a_running_caddy_or_alloy_is_recreated_to_read_its_new_config() -> None:
    """Both bind one file; replacing it changes the host inode, which a running container
    doesn't see (ADR-0023, ADR-0030)."""
    text = SCRIPT.read_text()
    assert "install -d -m 0750 /opt/ai-trainer/deploy/alloy" in text
    for service in ("caddy", "alloy"):
        assert f"ai-trainer-prod-{service}-1" in text
    assert "up -d --no-deps --force-recreate" in text
