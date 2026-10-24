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

SNAPSHOT = "00000003-0000001B-1"
COUNTS = "alembic_version 1\nconversations 3\nmessages 12\n"
# Exact rows per table, one `name count` line each, in one statement so a REPEATABLE READ
# transaction reads them all from one snapshot. The restore drill runs the same query (#224).
COUNT_SQL = (
    "SELECT format('%s %s', c.relname, (xpath('/row/n/text()', query_to_xml("
    "format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]) "
    "FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace "
    "WHERE s.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname;"
)

# Two commands. `psql` is the session holding the snapshot: it answers line by line, and its
# transaction is open (a `txn-open` file) from BEGIN to COMMIT; with FAKE_PSQL_FAIL it fails
# first, as a stopped database would. `pg_dump` is the dump: the marker repeated, so a
# plaintext copy anywhere on disk is easy to find, and it records whether the snapshot's
# transaction was open throughout. With FAKE_PG_DUMP_FAIL it stops halfway and fails, as a
# dropped connection would.
FAKE_DOCKER = f"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
log = Path(os.environ["FAKE_LOG"])
tool = "psql" if "psql" in sys.argv else "pg_dump"
(log / f"docker.{{tool}}.args").write_text(" ".join(sys.argv[1:]))
txn = log / "txn-open"
if tool == "psql":
    if os.environ.get("FAKE_PSQL_FAIL"):
        sys.stderr.write("psql: error: connection to server failed\\n")
        sys.exit(2)
    for line in sys.stdin:
        (log / "psql.sql").open("a").write(line)
        if line.startswith("BEGIN ISOLATION LEVEL REPEATABLE READ"):
            txn.touch()
        elif "pg_export_snapshot()" in line:
            print("{SNAPSHOT}", flush=True)
        elif "query_to_xml" in line:
            print(os.environ.get("FAKE_COUNTS", {COUNTS!r}), end="", flush=True)
        elif line.startswith("SELECT '-- end'"):
            print("-- end", flush=True)
        elif line.startswith("COMMIT"):
            txn.unlink()
    sys.exit(0)
open_at_start = txn.exists()
chunk = ("{MARKER}\\n" * 1000).encode()
for i in range(20):
    if i == 10 and os.environ.get("FAKE_PG_DUMP_FAIL"):
        sys.stderr.write("pg_dump: error: connection lost\\n")
        sys.exit(1)
    sys.stdout.buffer.write(chunk)
(log / "snapshot-held").write_text(str(open_at_start and txn.exists()))
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
# every file under the script's working directory and TMPDIR. With FAKE_RCLONE_FAIL (1, or an
# object name's suffix) it fails the way a refused request does, echoing credentials, which the
# script must not pass on.
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
fail = os.environ.get("FAKE_RCLONE_FAIL")
if fail and (fail == "1" or sys.argv[2].endswith(fail)):
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
    for name in ("log", "bucket", "tmp", "work", "local"):
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
        "BACKUP_LOCAL_DIR": str(tmp_path / "local"),
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


def _backup(prefix: str, stamp: str) -> list[str]:
    """One backup's objects: the counts manifest and the dump."""
    base = f"r2/ai-trainer-backups/{prefix}/ai-trainer-{stamp}"
    return [f"{base}.counts.age", f"{base}.dump.age"]


def _stored(env: dict[str, str], path: str) -> bytes:
    return (Path(env["FAKE_BUCKET"]) / "r2/ai-trainer-backups" / path).read_bytes()


def _decrypted(env: dict[str, str], path: str) -> str:
    """Undoes the fake age: drops its header and flips every byte back."""
    stored = _stored(env, path)
    header = b"age-encryption.org/v1\n"
    assert stored.startswith(header)
    return stored[len(header) :].translate(bytes(range(255, -1, -1))).decode()


def test_nightly_uploads_an_age_file_under_daily(env: dict[str, str]) -> None:
    result = _run(env, "nightly")

    assert result.returncode == 0, result.stderr
    assert _objects(env) == _backup("daily", "20261023T023000Z")
    stored = _stored(env, "daily/ai-trainer-20261023T023000Z.dump.age")
    assert stored.startswith(b"age-encryption.org/v1\n")
    assert MARKER.encode() not in stored
    assert "daily/ai-trainer-20261023T023000Z.dump.age" in result.stdout


def test_dumps_the_production_database_in_custom_format(env: dict[str, str]) -> None:
    assert _run(env, "nightly").returncode == 0

    args = (Path(env["FAKE_LOG"]) / "docker.pg_dump.args").read_text()
    assert args == (
        "exec ai-trainer-prod-db-1 pg_dump --format=custom --username=ai_trainer"
        f" --snapshot={SNAPSHOT} ai_trainer"
    )


def test_uploads_the_row_counts_encrypted_beside_the_dump(env: dict[str, str]) -> None:
    """The restore drill (#224) compares a restored backup against these counts."""
    result = _run(env, "nightly")

    assert result.returncode == 0, result.stderr
    assert _decrypted(env, "daily/ai-trainer-20261023T023000Z.counts.age") == COUNTS
    assert "daily/ai-trainer-20261023T023000Z.counts.age" in result.stdout


def test_counts_and_dump_read_the_same_snapshot(env: dict[str, str]) -> None:
    """Writes during the dump would otherwise make the drill report false mismatches."""
    assert _run(env, "nightly").returncode == 0

    log = Path(env["FAKE_LOG"])
    sql = (log / "psql.sql").read_text().splitlines()
    assert sql[0] == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"
    assert sql[1] == "SELECT pg_export_snapshot();"
    assert "query_to_xml" in sql[2]
    assert sql[-1] == "COMMIT;"
    assert f"--snapshot={SNAPSHOT}" in (log / "docker.pg_dump.args").read_text()
    # The exporting transaction stayed open until pg_dump had finished.
    assert (log / "snapshot-held").read_text() == "True"
    assert not (log / "txn-open").exists()


def test_counts_rows_with_the_query_the_drill_uses(env: dict[str, str]) -> None:
    assert _run(env, "nightly").returncode == 0

    count_sql = (Path(env["FAKE_LOG"]) / "psql.sql").read_text().splitlines()[2]
    assert count_sql == COUNT_SQL


def test_a_database_it_cannot_count_uploads_nothing(env: dict[str, str], tmp_path: Path) -> None:
    env["FAKE_PSQL_FAIL"] = "1"

    result = _run(env, "nightly")

    assert result.returncode != 0
    assert "connection to server failed" in result.stderr
    assert "backup: could not count rows" in result.stderr
    assert _objects(env) == []
    assert not (Path(env["FAKE_LOG"]) / "docker.pg_dump.args").exists()
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_failed_counts_upload_exits_non_zero_without_credentials(env: dict[str, str]) -> None:
    """The dump is already stored and locked; the failed unit makes the missing counts
    visible, and the drill refuses a backup without them."""
    env["FAKE_RCLONE_FAIL"] = ".counts.age"

    result = _run(env, "nightly")

    assert result.returncode != 0
    assert "backup: upload failed: r2:ai-trainer-backups/daily/" in result.stderr
    assert ".counts.age" in result.stderr
    output = result.stdout + result.stderr
    assert SECRET not in output
    assert KEY_ID not in output
    assert _objects(env) == ["r2/ai-trainer-backups/daily/ai-trainer-20261023T023000Z.dump.age"]


def test_the_db_container_can_be_the_local_stacks(env: dict[str, str]) -> None:
    env["BACKUP_DB_CONTAINER"] = "ai-trainer-db-1"

    assert _run(env, "nightly").returncode == 0

    log = Path(env["FAKE_LOG"])
    assert (log / "docker.pg_dump.args").read_text().startswith("exec ai-trainer-db-1 pg_dump ")
    assert (log / "docker.psql.args").read_text().startswith("exec -i ai-trainer-db-1 psql ")


def test_encrypts_to_the_owners_recipient_only(env: dict[str, str]) -> None:
    assert _run(env, "nightly").returncode == 0

    assert (Path(env["FAKE_LOG"]) / "age.args").read_text() == f"--encrypt --recipient {RECIPIENT}"


def test_sundays_nightly_goes_under_weekly(env: dict[str, str]) -> None:
    env["FAKE_NOW"] = "7 20261025T023000Z"

    assert _run(env, "nightly").returncode == 0

    assert _objects(env) == _backup("weekly", "20261025T023000Z")


def test_predeploy_goes_under_predeploy_on_any_day(env: dict[str, str]) -> None:
    env["FAKE_NOW"] = "7 20261025T101500Z"

    assert _run(env, "predeploy").returncode == 0

    assert _objects(env) == _backup("predeploy", "20261025T101500Z")


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


def _local(env: dict[str, str]) -> list[str]:
    return sorted(p.name for p in Path(env["BACKUP_LOCAL_DIR"]).iterdir())


def test_predeploy_keeps_the_same_encrypted_copy_on_the_server(env: dict[str, str]) -> None:
    """ADR-0023: the pre-migration dump is kept locally and uploaded (#225), encrypted like the
    upload, so a restore needs the owner's offline key either way."""
    assert _run(env, "predeploy").returncode == 0

    assert _local(env) == ["ai-trainer-20261023T023000Z.dump.age"]
    local = (Path(env["BACKUP_LOCAL_DIR"]) / _local(env)[0]).read_bytes()
    uploaded = (
        Path(env["FAKE_BUCKET"]) / "r2/ai-trainer-backups/predeploy" / _local(env)[0]
    ).read_bytes()
    assert local == uploaded
    assert MARKER.encode() not in local
    assert stat.S_IMODE((Path(env["BACKUP_LOCAL_DIR"]) / _local(env)[0]).stat().st_mode) == 0o600


def test_predeploy_keeps_only_the_newest_three_local_copies(env: dict[str, str]) -> None:
    local = Path(env["BACKUP_LOCAL_DIR"])
    for stamp in ("20261001T000000Z", "20261010T000000Z", "20261020T000000Z"):
        (local / f"ai-trainer-{stamp}.dump.age").write_bytes(b"old")
    (local / "notes.txt").write_text("not a backup")

    assert _run(env, "predeploy").returncode == 0

    assert _local(env) == [
        "ai-trainer-20261010T000000Z.dump.age",
        "ai-trainer-20261020T000000Z.dump.age",
        "ai-trainer-20261023T023000Z.dump.age",
        "notes.txt",
    ]


def test_nightly_keeps_no_local_copy(env: dict[str, str]) -> None:
    assert _run(env, "nightly").returncode == 0

    assert _local(env) == []


@pytest.mark.parametrize("failure", ["FAKE_PG_DUMP_FAIL", "FAKE_RCLONE_FAIL"])
def test_a_failed_predeploy_leaves_no_local_copy(env: dict[str, str], failure: str) -> None:
    env[failure] = "1"

    assert _run(env, "predeploy").returncode != 0

    assert _local(env) == []
