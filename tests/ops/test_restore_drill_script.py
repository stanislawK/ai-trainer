"""`deploy/restore-drill.sh` restores the latest backup into a throwaway Postgres and compares
row counts (ticket #224, ADR-0023 "Backups").

The script runs against stand-ins on PATH: `rclone` (a directory standing in for the bucket),
`age` (decrypts only with the test key), `docker` (the throwaway container) and `sleep`. The
fake dump's plaintext is the `name count` lines the fake database then reports, so a test
sets what the restore "contains". The real round trip is `make backup-check`."""

import os
import re
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.ops.test_backup_script import COUNT_SQL

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/restore-drill.sh"
KEY = "AGE-SECRET-KEY-1RESTOREDRILLTESTKEY"
HEADER = b"age-encryption.org/v1\n"
COUNTS = "alembic_version 1\nconversations 3\nmessages 12\n"
# Marks the fake dump, so a plaintext copy left on disk is easy to find.
MARKER = "-- PLAINTEXT-RESTORE-MARKER"

# The bucket: `lsf -R --files-only --include <glob>` and `cat <object>`, like rclone.
FAKE_RCLONE = """#!/usr/bin/env python3
import fnmatch, os, sys
from pathlib import Path
bucket = Path(os.environ["FAKE_BUCKET"])
def local(remote):
    name, path = remote.split(":", 1)
    return bucket / name / path
if sys.argv[1] == "lsf":
    assert sys.argv[2:6] == ["-R", "--files-only", "--include", "*.dump.age"], sys.argv
    root = local(sys.argv[6])
    for p in sorted(root.rglob("*")):
        if p.is_file() and fnmatch.fnmatch(p.name, "*.dump.age"):
            print(p.relative_to(root))
    sys.exit(0)
assert sys.argv[1] == "cat", sys.argv
path = local(sys.argv[2])
if not path.is_file():
    sys.stderr.write(f"ERROR : {sys.argv[2]}: object not found\\n")
    sys.exit(3)
sys.stdout.buffer.write(path.read_bytes())
"""

# Decrypts what the tests encrypt (a header, then every byte flipped), with the test key only.
FAKE_AGE = f"""#!/usr/bin/env python3
import sys
from pathlib import Path
assert sys.argv[1:3] == ["--decrypt", "--identity"], sys.argv
if "{KEY}" not in Path(sys.argv[3]).read_text().splitlines():
    sys.stderr.write("age: error: no identity matched any of the recipients\\n")
    sys.exit(1)
data = sys.stdin.buffer.read()
if not data.startswith({HEADER!r}):
    sys.stderr.write("age: error: failed to read header: parsing age header\\n")
    sys.exit(1)
sys.stdout.buffer.write(data[{len(HEADER)}:].translate(bytes(range(255, -1, -1))))
"""

# The container. `run` records its arguments; `exec … pg_isready` answers after
# FAKE_READY_AFTER tries (never with FAKE_NEVER_READY); `exec -i … pg_restore` keeps its stdin
# as the database's contents, in the log dir (the container), failing with FAKE_RESTORE_FAIL;
# with FAKE_RESTORE_HANG it runs until `rm` removes the container, as a real exec would;
# `exec … psql` prints those contents without the marker line.
FAKE_DOCKER = f"""#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
log = Path(os.environ["FAKE_LOG"])
args = sys.argv[1:]
with (log / "docker.calls").open("a") as f:
    f.write(" ".join(args) + "\\n")
if args[0] == "run":
    if os.environ.get("FAKE_RUN_FAIL"):
        sys.stderr.write("docker: Error response from daemon: pull access denied\\n")
        sys.exit(125)
    print("f00dfeed")
    sys.exit(0)
if args[0] == "rm":
    (log / "removed").touch()
    sys.exit(0)
assert args[0] == "exec", args
if "pg_isready" in args:
    tries = log / "ready.tries"
    n = int(tries.read_text()) + 1 if tries.exists() else 1
    tries.write_text(str(n))
    if os.environ.get("FAKE_NEVER_READY") or n < int(os.environ.get("FAKE_READY_AFTER", "1")):
        sys.exit(2)
    sys.exit(0)
if "pg_restore" in args:
    data = sys.stdin.buffer.read()
    if os.environ.get("FAKE_RESTORE_HANG"):
        (log / "restore.started").touch()
        deadline = time.monotonic() + 20
        while not (log / "removed").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        sys.stderr.write("Error: container removed\\n")
        sys.exit(137)
    if os.environ.get("FAKE_RESTORE_FAIL"):
        sys.stderr.write("pg_restore: error: type vector does not exist\\n")
        sys.exit(1)
    (log / "database").write_bytes(data)
    sys.exit(0)
assert "psql" in args, args
lines = (log / "database").read_text().splitlines(keepends=True)
print("".join(l for l in lines if l.strip() != "{MARKER}"), end="")
"""

FAKE_SLEEP = """#!/bin/sh
exit 0
"""


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _encrypt(text: str) -> bytes:
    return HEADER + text.encode().translate(bytes(range(255, -1, -1)))


def _put_backup(
    env: dict[str, str],
    path: str,
    restored: str = COUNTS,
    counts: str | None = COUNTS,
) -> None:
    """Stores a dump whose restore holds `restored`, and its counts manifest unless None."""
    bucket = Path(env["FAKE_BUCKET"]) / "r2/ai-trainer-backups"
    dump = bucket / path
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_bytes(_encrypt(f"{MARKER}\n{restored}"))
    if counts is not None:
        manifest = bucket / path.replace(".dump.age", ".counts.age")
        manifest.write_bytes(_encrypt(counts))


@pytest.fixture(scope="module")
def bin_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The stand-ins, written once per module: macOS scans a new executable on its first run,
    one at a time across processes, which made each test wait (#328)."""
    bin_dir = tmp_path_factory.mktemp("bin")
    for name, body in (
        ("rclone", FAKE_RCLONE),
        ("age", FAKE_AGE),
        ("docker", FAKE_DOCKER),
        ("sleep", FAKE_SLEEP),
    ):
        _stub(bin_dir, name, body)
    return bin_dir


@pytest.fixture
def env(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    for name in ("log", "bucket", "tmp", "work"):
        (tmp_path / name).mkdir()
    key = tmp_path / "ai-trainer-backup.key"
    key.write_text(f"# created: test\n{KEY}\n")
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path / "tmp"),
        "FAKE_LOG": str(tmp_path / "log"),
        "FAKE_BUCKET": str(tmp_path / "bucket"),
        "BACKUP_DEST": "r2:ai-trainer-backups",
        "RESTORE_AGE_IDENTITY": str(key),
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


def _calls(env: dict[str, str]) -> list[str]:
    path = Path(env["FAKE_LOG"]) / "docker.calls"
    return path.read_text().splitlines() if path.exists() else []


def _container(env: dict[str, str]) -> str:
    run = next(c for c in _calls(env) if c.startswith("run "))
    match = re.search(r"--name (\S+)", run)
    assert match
    return match.group(1)


def _rows(stdout: str) -> dict[str, list[str]]:
    """The comparison table: table name → [backup, restored, verdict]."""
    rows = {}
    for line in stdout.splitlines():
        cells = line.split()
        if len(cells) == 4 and cells[3] in ("ok", "MISMATCH"):
            rows[cells[0]] = cells[1:]
    return rows


def test_restores_the_latest_backup_and_every_count_matches(env: dict[str, str]) -> None:
    _put_backup(env, "weekly/ai-trainer-20261018T023000Z.dump.age", counts="stale 1\n")
    _put_backup(env, "predeploy/ai-trainer-20261022T101500Z.dump.age", counts="stale 2\n")
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert "daily/ai-trainer-20261023T023000Z.dump.age" in result.stdout
    assert _rows(result.stdout) == {
        "alembic_version": ["1", "1", "ok"],
        "conversations": ["3", "3", "ok"],
        "messages": ["12", "12", "ok"],
    }
    assert "restore-drill: every count matches (3 tables)" in result.stdout


def test_the_latest_is_the_newest_stamp_in_any_prefix(env: dict[str, str]) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age", counts="stale 1\n")
    _put_backup(env, "weekly/ai-trainer-20261025T023000Z.dump.age", counts="stale 2\n")
    _put_backup(env, "predeploy/ai-trainer-20261025T101500Z.dump.age")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert "predeploy/ai-trainer-20261025T101500Z.dump.age" in result.stdout


def test_a_named_backup_is_drilled_instead_of_the_latest(env: dict[str, str]) -> None:
    _put_backup(env, "weekly/ai-trainer-20261018T023000Z.dump.age")
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age", counts="stale 1\n")

    result = _run(env, "weekly/ai-trainer-20261018T023000Z.dump.age")

    assert result.returncode == 0, result.stderr
    assert "weekly/ai-trainer-20261018T023000Z.dump.age" in result.stdout


def test_a_count_mismatch_exits_non_zero(env: dict[str, str]) -> None:
    _put_backup(
        env,
        "daily/ai-trainer-20261023T023000Z.dump.age",
        restored="alembic_version 1\nconversations 3\nmessages 11\n",
    )

    result = _run(env)

    assert result.returncode == 1
    assert _rows(result.stdout)["messages"] == ["12", "11", "MISMATCH"]
    assert _rows(result.stdout)["conversations"] == ["3", "3", "ok"]
    assert "restore-drill: 1 of 3 tables differ" in result.stderr


@pytest.mark.parametrize(
    ("restored", "table", "row"),
    [
        ("alembic_version 1\nconversations 3\n", "messages", ["12", "-", "MISMATCH"]),
        (COUNTS + "venues 0\n", "venues", ["-", "0", "MISMATCH"]),
    ],
    ids=["missing-from-restore", "missing-from-backup-counts"],
)
def test_a_table_on_one_side_only_is_a_mismatch(
    env: dict[str, str], restored: str, table: str, row: list[str]
) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age", restored=restored)

    result = _run(env)

    assert result.returncode == 1
    assert _rows(result.stdout)[table] == row


def test_counts_that_list_no_tables_fail(env: dict[str, str]) -> None:
    """Every real backup holds alembic_version; an empty list means the counts are wrong."""
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age", restored="", counts="")

    result = _run(env)

    assert result.returncode == 1
    assert "no tables" in result.stderr


@pytest.mark.parametrize(
    ("counts", "restored"),
    [
        ("not counts at all\n", COUNTS),
        (COUNTS, "garbage\n"),
        ("users 5\nusers 6\n", "users 6\n"),
        ("users x\n", "users x\n"),
    ],
    ids=["unreadable-counts", "unreadable-restore", "duplicate-table", "not-a-number"],
)
def test_counts_it_cannot_read_fail(env: dict[str, str], counts: str, restored: str) -> None:
    """A line that isn't `table count` is never skipped: skipped on both sides, it would let
    a broken manifest pass as matching."""
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age", restored=restored, counts=counts)

    result = _run(env)

    assert result.returncode == 1
    assert "every count matches" not in result.stdout


def test_a_key_that_cannot_decrypt_exits_non_zero(env: dict[str, str]) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")
    Path(env["RESTORE_AGE_IDENTITY"]).write_text("AGE-SECRET-KEY-1SOMEONEELSESKEY\n")

    result = _run(env)

    assert result.returncode == 1
    assert "no identity matched" in result.stderr
    assert "restore-drill: could not fetch or decrypt" in result.stderr


def test_a_dump_that_fails_to_decrypt_exits_non_zero(env: dict[str, str]) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")
    bucket = Path(env["FAKE_BUCKET"]) / "r2/ai-trainer-backups"
    (bucket / "daily/ai-trainer-20261023T023000Z.dump.age").write_bytes(b"not an age file")

    result = _run(env)

    assert result.returncode == 1
    assert "parsing age header" in result.stderr
    assert "restore-drill: restore failed" in result.stderr


def test_a_failed_restore_exits_non_zero(env: dict[str, str]) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")
    env["FAKE_RESTORE_FAIL"] = "1"

    result = _run(env)

    assert result.returncode == 1
    assert "type vector does not exist" in result.stderr
    assert "restore-drill: restore failed" in result.stderr


def test_a_backup_without_counts_exits_non_zero(env: dict[str, str]) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age", counts=None)

    result = _run(env)

    assert result.returncode == 1
    assert "object not found" in result.stderr
    assert "restore-drill: could not fetch or decrypt" in result.stderr
    # Nothing was started for a backup it can't check.
    assert _calls(env) == []


def test_an_empty_bucket_exits_non_zero(env: dict[str, str]) -> None:
    result = _run(env)

    assert result.returncode == 1
    assert "restore-drill: no backup found in r2:ai-trainer-backups" in result.stderr
    assert _calls(env) == []


def test_waits_for_postgres_to_accept_tcp(env: dict[str, str]) -> None:
    """The image's first-start init server listens on its socket only, so a TCP check can't
    pass before the real server is up."""
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")
    env["FAKE_READY_AFTER"] = "4"

    result = _run(env)

    assert result.returncode == 0, result.stderr
    checks = [c for c in _calls(env) if "pg_isready" in c]
    assert len(checks) == 4
    assert all("-h 127.0.0.1" in c for c in checks)


def test_postgres_that_never_starts_exits_non_zero(env: dict[str, str]) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")
    env["FAKE_NEVER_READY"] = "1"

    result = _run(env)

    assert result.returncode == 1
    assert "restore-drill: Postgres did not start" in result.stderr
    assert not any("pg_restore" in c for c in _calls(env))


def test_the_container_is_offline_in_memory_and_runs_productions_image(
    env: dict[str, str],
) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")

    assert _run(env).returncode == 0

    run = next(c for c in _calls(env) if c.startswith("run "))
    assert " --network none " in run
    assert " --tmpfs /var/lib/postgresql " in run
    prod_image = re.search(
        r"^  db:\n(?:    .*\n)*?    image: (\S+)$",
        (ROOT / "compose.prod.yaml").read_text(),
        re.MULTILINE,
    )
    assert prod_image
    assert run.endswith(f" {prod_image.group(1)}")
    assert "@sha256:" in run


def test_restores_in_one_transaction_and_counts_with_the_backups_query(
    env: dict[str, str],
) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")

    assert _run(env).returncode == 0

    name = _container(env)
    restore = next(c for c in _calls(env) if "pg_restore" in c)
    assert restore == (
        f"exec -i {name} pg_restore --single-transaction --no-owner"
        " --username=ai_trainer --dbname=ai_trainer"
    )
    count = next(c for c in _calls(env) if " psql " in c)
    assert count.endswith(f" -c {COUNT_SQL}")


SCENARIOS = {
    "match": {},
    "mismatch": {"restored": "messages 1\n"},
    "restore-fails": {"FAKE_RESTORE_FAIL": "1"},
    "never-ready": {"FAKE_NEVER_READY": "1"},
    "dump-undecryptable": {"corrupt": "1"},
}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_removes_the_container_and_leaves_no_plaintext(
    env: dict[str, str], tmp_path: Path, scenario: str
) -> None:
    settings = dict(SCENARIOS[scenario])
    _put_backup(
        env, "daily/ai-trainer-20261023T023000Z.dump.age", restored=settings.pop("restored", COUNTS)
    )
    if settings.pop("corrupt", None):
        dump = tmp_path / "bucket/r2/ai-trainer-backups/daily/ai-trainer-20261023T023000Z.dump.age"
        dump.write_bytes(b"garbage")
    env.update(settings)

    _run(env)

    name = _container(env)
    assert name.startswith("ai-trainer-restore-drill-")
    assert _calls(env)[-1] == f"rm -f -v {name}"
    assert list((tmp_path / "tmp").iterdir()) == []
    assert list((tmp_path / "work").iterdir()) == []


def test_a_container_that_fails_to_start_is_still_removed(env: dict[str, str]) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")
    env["FAKE_RUN_FAIL"] = "1"

    result = _run(env)

    assert result.returncode == 1
    assert "pull access denied" in result.stderr
    assert _calls(env)[-1] == f"rm -f -v {_container(env)}"


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_an_interrupted_drill_removes_the_container(
    env: dict[str, str], tmp_path: Path, sig: signal.Signals
) -> None:
    _put_backup(env, "daily/ai-trainer-20261023T023000Z.dump.age")
    env["FAKE_RESTORE_HANG"] = "1"
    started = tmp_path / "log/restore.started"
    proc = subprocess.Popen(
        [str(SCRIPT)],
        env=env,
        cwd=tmp_path / "work",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while not started.exists():
        assert time.monotonic() < deadline, "the restore never started"
        time.sleep(0.05)

    # Ctrl-C reaches the whole foreground process group; a TERM, the script alone.
    if sig == signal.SIGINT:
        os.killpg(proc.pid, sig)
    else:
        proc.send_signal(sig)
    proc.communicate(timeout=15)

    assert proc.returncode != 0
    assert _calls(env)[-1] == f"rm -f -v {_container(env)}"


@pytest.mark.parametrize("name", ["BACKUP_DEST", "RESTORE_AGE_IDENTITY"])
def test_refuses_to_run_without_its_settings(env: dict[str, str], name: str) -> None:
    del env[name]

    result = _run(env)

    assert result.returncode == 2
    assert name in result.stderr
    assert _calls(env) == []


def test_refuses_a_key_file_it_cannot_read(env: dict[str, str], tmp_path: Path) -> None:
    env["RESTORE_AGE_IDENTITY"] = str(tmp_path / "missing.key")

    result = _run(env)

    assert result.returncode == 2
    assert "RESTORE_AGE_IDENTITY" in result.stderr
    assert _calls(env) == []
