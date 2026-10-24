"""`deploy/backup.sh` streams `pg_dump` through age into R2 (ticket #223, ADR-0023 "Backups").

The script runs against stand-ins on PATH: `docker` (the dump), `age`, `rclone` (the bucket) and
`date`. The real round trip — a real key, a real dump, `pg_restore --list` — is
`make backup-check` against the local stack, since age and rclone aren't in CI."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/backup.sh"
# The example recipient from age's README.
RECIPIENT = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
MARKER = "PLAINTEXT-DUMP-MARKER"
SECRET = "r2-secret-access-key-do-not-log"
KEY_ID = "r2-access-key-id-do-not-log"

# The dump: the marker repeated, so a plaintext copy anywhere on disk is easy to find. With
# FAKE_PG_DUMP_FAIL it stops halfway and fails, as a dropped connection would.
FAKE_DOCKER = f"""#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_LOG"] + "/docker.args", "w") as f:
    f.write(" ".join(sys.argv[1:]))
chunk = ("{MARKER}\\n" * 1000).encode()
for i in range(20):
    if i == 10 and os.environ.get("FAKE_PG_DUMP_FAIL"):
        sys.stderr.write("pg_dump: error: connection lost\\n")
        sys.exit(1)
    sys.stdout.buffer.write(chunk)
"""

# Stands in for encryption: a header, then the input with every byte flipped, so the marker
# can't survive it.
FAKE_AGE = """#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_LOG"] + "/age.args", "w") as f:
    f.write(" ".join(sys.argv[1:]))
sys.stdout.buffer.write(b"age-encryption.org/v1\\n")
while block := sys.stdin.buffer.read(65536):
    sys.stdout.buffer.write(block.translate(bytes(range(255, -1, -1))))
"""

# The bucket. Like a multipart upload, the object appears only once stdin ends; killed before
# that, nothing is stored. Early in the stream and again at its end it looks for the marker in
# every file under the script's working directory and TMPDIR. With FAKE_RCLONE_FAIL it fails the
# way a refused request does, echoing credentials, which the script must not pass on.
FAKE_RCLONE = """#!/usr/bin/env python3
import os, sys
from pathlib import Path
log = Path(os.environ["FAKE_LOG"])
(log / "rclone.args").write_text(" ".join(sys.argv[1:]))
assert sys.argv[1] == "rcat", sys.argv
seen = set()
def scan():
    for top in (os.getcwd(), os.environ.get("TMPDIR", "/tmp")):
        for dirpath, _, files in os.walk(top):
            for name in files:
                path = Path(dirpath, name)
                try:
                    if path.is_file() and b"PLAINTEXT-DUMP-MARKER" in path.read_bytes():
                        seen.add(str(path))
                except OSError:
                    pass
    (log / "plaintext-on-disk").write_text("\\n".join(sorted(seen)))
data = sys.stdin.buffer.read(4096)
scan()
if os.environ.get("FAKE_RCLONE_FAIL"):
    key = os.environ.get("RCLONE_CONFIG_R2_ACCESS_KEY_ID", "")
    secret = os.environ.get("RCLONE_CONFIG_R2_SECRET_ACCESS_KEY", "")
    sys.stderr.write(f"ERROR : Failed to rcat: AccessDenied: key {key} secret {secret}\\n")
    sys.exit(1)
while block := sys.stdin.buffer.read(65536):
    data += block
scan()
remote, path = sys.argv[2].split(":", 1)
target = Path(os.environ["FAKE_BUCKET"], remote, path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(data)
"""

FAKE_DATE = """#!/bin/sh
echo "$FAKE_NOW"
"""


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("docker", FAKE_DOCKER),
        ("age", FAKE_AGE),
        ("rclone", FAKE_RCLONE),
        ("date", FAKE_DATE),
    ):
        _stub(bin_dir, name, body)
    for name in ("log", "bucket", "tmp", "work"):
        (tmp_path / name).mkdir()
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path / "tmp"),
        "FAKE_LOG": str(tmp_path / "log"),
        "FAKE_BUCKET": str(tmp_path / "bucket"),
        # A Friday.
        "FAKE_NOW": "5 20261023T023000Z",
        "BACKUP_DEST": "r2:ai-trainer-backups",
        "BACKUP_AGE_RECIPIENT": RECIPIENT,
        "RCLONE_CONFIG_R2_ACCESS_KEY_ID": KEY_ID,
        "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": SECRET,
    }


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        env=env,
        cwd=Path(env["FAKE_LOG"]).parent / "work",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _objects(env: dict[str, str]) -> list[str]:
    bucket = Path(env["FAKE_BUCKET"])
    return sorted(str(p.relative_to(bucket)) for p in bucket.rglob("*") if p.is_file())


def test_nightly_uploads_an_age_file_under_daily(env: dict[str, str]) -> None:
    result = _run(env, "nightly")

    assert result.returncode == 0, result.stderr
    assert _objects(env) == ["r2/ai-trainer-backups/daily/ai-trainer-20261023T023000Z.dump.age"]
    stored = (Path(env["FAKE_BUCKET"]) / "r2" / _objects(env)[0].split("/", 1)[1]).read_bytes()
    assert stored.startswith(b"age-encryption.org/v1\n")
    assert MARKER.encode() not in stored
    assert "daily/ai-trainer-20261023T023000Z.dump.age" in result.stdout


def test_dumps_the_production_database_in_custom_format(env: dict[str, str]) -> None:
    assert _run(env, "nightly").returncode == 0

    args = (Path(env["FAKE_LOG"]) / "docker.args").read_text()
    assert args == (
        "exec ai-trainer-prod-db-1 pg_dump --format=custom --username=ai_trainer ai_trainer"
    )


def test_the_db_container_can_be_the_local_stacks(env: dict[str, str]) -> None:
    env["BACKUP_DB_CONTAINER"] = "ai-trainer-db-1"

    assert _run(env, "nightly").returncode == 0

    assert (
        (Path(env["FAKE_LOG"]) / "docker.args")
        .read_text()
        .startswith("exec ai-trainer-db-1 pg_dump ")
    )


def test_encrypts_to_the_owners_recipient_only(env: dict[str, str]) -> None:
    assert _run(env, "nightly").returncode == 0

    assert (Path(env["FAKE_LOG"]) / "age.args").read_text() == f"--encrypt --recipient {RECIPIENT}"


def test_sundays_nightly_goes_under_weekly(env: dict[str, str]) -> None:
    env["FAKE_NOW"] = "7 20261025T023000Z"

    assert _run(env, "nightly").returncode == 0

    assert _objects(env) == ["r2/ai-trainer-backups/weekly/ai-trainer-20261025T023000Z.dump.age"]


def test_predeploy_goes_under_predeploy_on_any_day(env: dict[str, str]) -> None:
    env["FAKE_NOW"] = "7 20261025T101500Z"

    assert _run(env, "predeploy").returncode == 0

    assert _objects(env) == ["r2/ai-trainer-backups/predeploy/ai-trainer-20261025T101500Z.dump.age"]


def test_writes_no_plaintext_dump_to_disk(env: dict[str, str], tmp_path: Path) -> None:
    result = _run(env, "nightly")

    assert result.returncode == 0, result.stderr
    # rclone looked while the dump was streaming through.
    assert (Path(env["FAKE_LOG"]) / "plaintext-on-disk").read_text() == ""
    # Nothing is left behind in TMPDIR or the working directory either.
    assert list((tmp_path / "tmp").iterdir()) == []
    assert list((tmp_path / "work").iterdir()) == []


# rclone fails while the dump is still streaming, so age dies of SIGPIPE and the pipeline
# returns before bash has reaped rclone. Repeated, since that ordering is a race.
@pytest.mark.parametrize("attempt", range(10))
def test_a_failed_upload_exits_non_zero_with_its_reason_and_no_credentials(
    env: dict[str, str], tmp_path: Path, attempt: int
) -> None:
    env["FAKE_RCLONE_FAIL"] = "1"

    result = _run(env, "nightly")

    assert result.returncode != 0
    assert "backup: upload failed" in result.stderr
    assert "dump or encryption failed" not in result.stderr
    assert "AccessDenied" in result.stderr
    output = result.stdout + result.stderr
    assert SECRET not in output
    assert KEY_ID not in output
    assert _objects(env) == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_dump_that_fails_midway_uploads_nothing(env: dict[str, str], tmp_path: Path) -> None:
    """A truncated object would sit under the bucket lock for its whole window, so rclone is
    stopped before it sees the end of its input."""
    env["FAKE_PG_DUMP_FAIL"] = "1"

    result = _run(env, "nightly")

    assert result.returncode != 0
    assert "backup: dump or encryption failed" in result.stderr
    assert "connection lost" in result.stderr
    assert _objects(env) == []
    assert list((tmp_path / "tmp").iterdir()) == []


@pytest.mark.parametrize("args", [(), ("hourly",), ("nightly", "extra")])
def test_rejects_an_unknown_kind(env: dict[str, str], args: tuple[str, ...]) -> None:
    result = _run(env, *args)

    assert result.returncode == 2
    assert "usage: backup.sh nightly|predeploy" in result.stderr
    assert _objects(env) == []


@pytest.mark.parametrize("name", ["BACKUP_DEST", "BACKUP_AGE_RECIPIENT"])
def test_refuses_to_run_without_its_settings(env: dict[str, str], name: str) -> None:
    del env[name]

    result = _run(env, "nightly")

    assert result.returncode == 2
    assert name in result.stderr
    assert _objects(env) == []


def test_refuses_a_recipient_that_is_not_an_age_public_key(env: dict[str, str]) -> None:
    """A private key (AGE-SECRET-KEY-…) pasted by mistake must never reach the server's logs
    or argv, nor anything that isn't an X25519 recipient."""
    env["BACKUP_AGE_RECIPIENT"] = "AGE-SECRET-KEY-1QQQQ"

    result = _run(env, "nightly")

    assert result.returncode == 2
    assert "BACKUP_AGE_RECIPIENT" in result.stderr
    assert "AGE-SECRET-KEY" not in result.stderr
    assert not (Path(env["FAKE_LOG"]) / "age.args").exists()
