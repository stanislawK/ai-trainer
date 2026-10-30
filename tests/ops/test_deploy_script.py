"""`deploy/deploy.sh` runs an approved release on the server (ticket #225, ADR-0023 "Pull-based
deploy on the server", invariants 2, 3 and 5; ADR-0024 invariant 4).

The script runs against stand-ins on PATH: `gh` (the deployments API and `gh attestation
verify`), `docker` (pull, compose, and the health check from the Caddy container), `curl`
(which must never be called: the check stays off Cloudflare) and `sleep`, plus a stand-in
`backup.sh` beside it. Every stand-in appends one JSON line per call to a shared log, so the
tests can check the order of the steps and what each one was given. The real run is the
owner's, on the host (deploy/RUNBOOK.md, section 10)."""

import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/deploy.sh"
REPO = "stanislawK/ai-trainer"
IMAGE = "ghcr.io/stanislawk/ai-trainer"
NEW = "sha256:" + "a" * 64
OLD = "sha256:" + "b" * 64
TOKEN = "github_pat_do-not-log-this-token"
MONITOR_PASSWORD = "0123456789abcdef0123456789abcdef"
SITE_HOST = "trainer.example.com"
BOT = {"login": "github-actions[bot]", "type": "Bot"}
OWNER = {"login": "stanislawK", "type": "User"}

# Shared by every stand-in: one JSON line per call, with the arguments, APP_IMAGE, whether
# GH_TOKEN is the token, and whether the token appears anywhere else in the environment.
LOG_CALL = """
import json, os, sys
from pathlib import Path
state = Path(os.environ["FAKE_STATE"])
def log_call(tool):
    env = dict(os.environ)
    gh_token = env.pop("GH_TOKEN", None)
    entry = {
        "tool": tool,
        "args": sys.argv[1:],
        "app_image": env.get("APP_IMAGE"),
        "release_tag": env.get("RELEASE_TAG"),
        "gh_token_is_the_token": gh_token == os.environ["FAKE_TOKEN"],
        "token_elsewhere": any(os.environ["FAKE_TOKEN"] in v for k, v in env.items()
                               if k != "FAKE_TOKEN"),
    }
    with open(state / "calls.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\\n")
"""

# The deployments API and `gh attestation verify`. `--jq` goes through the real jq, raw like
# gh's own. Statuses are kept newest first, as the API returns them.
FAKE_GH = (
    "#!/usr/bin/env python3"
    + LOG_CALL
    + """
import subprocess
log_call("gh")
args = sys.argv[1:]
if args[0] == "attestation":
    tries = state / "attest-tries"
    tried = int(tries.read_text()) if tries.exists() else 0
    tries.write_text(str(tried + 1))
    failing = int(os.environ.get("FAKE_ATTEST_FAIL_TIMES", 0))
    if os.environ.get("FAKE_ATTEST_FAIL") or tried < failing:
        sys.stderr.write("Error: no matching attestations found\\n")
        sys.exit(1)
    print("Verification succeeded!")
    sys.exit(0)
assert args[0] == "api", args
method, fields, jq, path = "GET", {}, None, None
rest = args[1:]
while rest:
    arg = rest.pop(0)
    if arg in ("--method", "-X"):
        method = rest.pop(0)
    elif arg in ("-f", "-F", "--raw-field", "--field"):
        key, value = rest.pop(0).split("=", 1)
        fields[key] = value
    elif arg in ("--jq", "-q"):
        jq = rest.pop(0)
    else:
        path = arg
statuses = json.loads((state / "statuses.json").read_text())
prefix = "repos/stanislawK/ai-trainer/deployments"
if method == "POST":
    if os.environ.get("FAKE_GH_POST_FAIL") == fields.get("state"):
        sys.stderr.write("gh: HTTP 502\\n")
        sys.exit(1)
    deployment_id = path.removeprefix(prefix + "/").removesuffix("/statuses")
    status = {**fields, "creator": {"login": "stanislawK", "type": "User"}}
    statuses.setdefault(deployment_id, []).insert(0, status)
    (state / "statuses.json").write_text(json.dumps(statuses))
    with open(state / "posted.jsonl", "a") as f:
        f.write(json.dumps({"id": deployment_id, **fields}) + "\\n")
    data = {"id": 1}
elif path.startswith(prefix + "?"):
    assert "environment=production" in path, path
    data = json.loads((state / "deployments.json").read_text())
else:
    deployment_id = path.removeprefix(prefix + "/").split("/statuses", 1)[0]
    data = statuses.get(deployment_id, [])
out = json.dumps(data)
if jq:
    out = subprocess.run(["jq", "-r", jq], input=out, capture_output=True, text=True,
                         check=True).stdout
sys.stdout.write(out if out.endswith("\\n") or not out else out + "\\n")
"""
)

# `docker pull` and `docker compose`. `up` records the image the app serves; `up --wait` fails
# for an image listed in FAKE_UNHEALTHY, as a failing container healthcheck would.
# `exec caddy wget` is the health check. Like busybox wget, it fails while the app serves an
# image listed in FAKE_UNHEALTHY or FAKE_HEALTH_UNHEALTHY (unhealthy only to the poll). For one
# listed in FAKE_DB_UNAVAILABLE it answers 200 with the body /health gives when Postgres is
# unreachable.
FAKE_DOCKER = (
    "#!/usr/bin/env python3"
    + LOG_CALL
    + """
log_call("docker")
args = sys.argv[1:]
unhealthy = os.environ.get("FAKE_UNHEALTHY", "").split(",")
def hang(step):
    if os.environ.get("FAKE_HANG") == step:
        (state / "hanging").touch()
        import time
        time.sleep(60)
if args[0] == "pull":
    hang("pull")
    sys.exit(1 if os.environ.get("FAKE_PULL_FAIL") else 0)
assert args[0] == "compose", args
if "run" in args and "migrate" in args:
    if os.environ.get("FAKE_MIGRATE_FAIL"):
        sys.stderr.write("alembic: FAILED: relation already exists\\n")
        sys.exit(1)
if "exec" in args and "psql" in args:
    with open(state / "psql-stdin", "a") as f:
        f.write(sys.stdin.read())
    if os.environ.get("FAKE_MONITOR_FAIL"):
        sys.stderr.write('ERROR:  role "ai_trainer_monitor" does not exist\\n')
        sys.exit(3)
if "exec" in args and "wget" in args:
    serving = (state / "serving").read_text() if (state / "serving").exists() else ""
    bad = unhealthy + os.environ.get("FAKE_HEALTH_UNHEALTHY", "").split(",")
    if serving in bad:
        sys.stderr.write("wget: server returned error: HTTP/1.1 503 Service Unavailable\\n")
        sys.exit(1)
    no_db = serving in os.environ.get("FAKE_DB_UNAVAILABLE", "").split(",")
    print(json.dumps({"database": "unavailable" if no_db else "ok"}, separators=(",", ":")))
if "up" in args and "alloy" in args and os.environ.get("FAKE_ALLOY_FAIL"):
    sys.stderr.write("Error response from daemon: alloy failed to start\\n")
    sys.exit(1)
if "up" in args and "app" in args:
    (state / "serving").write_text(os.environ["APP_IMAGE"])
    if os.environ["APP_IMAGE"].endswith(os.environ.get("FAKE_HANG_DIGEST", "-")):
        hang("app")
    if "--wait" in args and os.environ["APP_IMAGE"] in unhealthy:
        sys.stderr.write("container ai-trainer-prod-app-1 is unhealthy\\n")
        sys.exit(1)
"""
)

# Logs only: the health check never goes through Cloudflare (ADR-0023, amended 2026-10-28).
FAKE_CURL = (
    "#!/usr/bin/env python3"
    + LOG_CALL
    + """
log_call("curl")
sys.exit(7)
"""
)

FAKE_BACKUP = (
    "#!/usr/bin/env python3"
    + LOG_CALL
    + """
log_call("backup.sh")
if os.environ.get("FAKE_BACKUP_FAIL"):
    sys.stderr.write("backup: upload failed: r2:ai-trainer-backups/predeploy/x.dump.age\\n")
    sys.exit(1)
print("backup: uploaded r2:ai-trainer-backups/predeploy/x.dump.age")
"""
)

FAKE_SLEEP = "#!/bin/sh\nexit 0\n"

# Only where util-linux flock is missing (macOS): `flock -n <fd>` on the inherited descriptor,
# which holds the lock for the parent shell just as flock(1) does.
FAKE_FLOCK = """#!/usr/bin/env python3
import fcntl, sys
assert sys.argv[1] == "-n", sys.argv
try:
    fcntl.flock(int(sys.argv[2]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
"""


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _deployment(
    deployment_id: int, digest: str = NEW, creator: dict[str, str] = BOT, ref: str = "v0.2.0"
) -> dict[str, Any]:
    return {
        "id": deployment_id,
        "ref": ref,
        "environment": "production",
        "creator": creator,
        "payload": {"digest": digest},
    }


class Server:
    """The host as the script sees it: /opt/ai-trainer, the credential, and the API's state."""

    def __init__(self, tmp_path: Path, tools: Path) -> None:
        self.root = tmp_path / "opt"
        self.state = tmp_path / "state"
        creds = tmp_path / "creds"
        for directory in (self.root, creds, self.state):
            directory.mkdir(parents=True)
        self.script = tools / "deploy" / "deploy.sh"
        bin_dir = tools / "bin"
        (creds / "github-token").write_text(TOKEN + "\n")
        (self.root / "app-image").write_text(f"{IMAGE}@{OLD}\n")
        (self.root / "app-version").write_text("v0.1.0\n")
        (self.root / "alloy.env").write_text(f"POSTGRES_MONITOR_PASSWORD={MONITOR_PASSWORD}\n")
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "DEPLOY_ROOT": str(self.root),
            "CREDENTIALS_DIRECTORY": str(creds),
            "SITE_HOST": SITE_HOST,
            "FAKE_STATE": str(self.state),
            "FAKE_TOKEN": TOKEN,
        }
        self.publish([_deployment(7)], {"7": ["queued"]})

    def publish(
        self,
        deployments: list[dict[str, Any]],
        states: dict[str, list[str]],
        status_creator: dict[str, str] = BOT,
    ) -> None:
        """`states` newest first, each posted by `status_creator` (the release job's queued)."""
        (self.state / "deployments.json").write_text(json.dumps(deployments))
        statuses = {
            key: [{"state": s, "creator": status_creator} for s in value]
            for key, value in states.items()
        }
        (self.state / "statuses.json").write_text(json.dumps(statuses))

    def run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script)],
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        path = self.state / "calls.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        calls = [json.loads(line) for line in lines]
        return [c for c in calls if tool is None or c["tool"] == tool]

    def posted(self) -> list[dict[str, str]]:
        path = self.state / "posted.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [json.loads(line) for line in lines]

    def steps(self) -> list[str]:
        """Each call reduced to a step name, in order."""
        names = []
        for call in self.calls():
            args = call["args"]
            if call["tool"] == "gh":
                if args[0] == "attestation":
                    names.append("verify")
                elif "POST" in args:
                    state = next(a for a in args if a.startswith("state=")).split("=", 1)[1]
                    names.append(f"status:{state}")
                else:
                    names.append("read")
            elif call["tool"] == "docker":
                if args[0] == "pull":
                    names.append("pull")
                elif "migrate" in args:
                    names.append("migrate")
                elif "exec" in args and "psql" in args:
                    names.append("monitor-password")
                elif "exec" in args and "wget" in args:
                    names.append("health")
                elif "up" in args:
                    names.append(
                        "up:" + "+".join(a for a in ("db", "app", "caddy", "alloy") if a in args)
                    )
            else:
                names.append(call["tool"])
        return names

    def serving(self) -> str:
        return (self.state / "serving").read_text()

    def recorded_image(self) -> str:
        return (self.root / "app-image").read_text().strip()

    def recorded_version(self) -> str:
        return (self.root / "app-version").read_text().strip()

    def app_starts(self) -> list[dict[str, Any]]:
        """Each `compose up` of the app, in order."""
        return [c for c in self.calls("docker") if "up" in c["args"] and "app" in c["args"]]


@pytest.fixture(scope="module")
def tools(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The script and its stand-ins, written once per module: macOS scans a new executable on
    its first run, one at a time across processes, which made each test wait (#328)."""
    tools = tmp_path_factory.mktemp("deploy-tools")
    scripts = tools / "deploy"
    bin_dir = tools / "bin"
    for directory in (scripts, bin_dir):
        directory.mkdir()
    shutil.copy(SCRIPT, scripts / "deploy.sh")
    shutil.copy(ROOT / "deploy/monitor-password.sh", scripts / "monitor-password.sh")
    _stub(scripts, "backup.sh", FAKE_BACKUP)
    for name, body in (
        ("gh", FAKE_GH),
        ("docker", FAKE_DOCKER),
        ("curl", FAKE_CURL),
        ("sleep", FAKE_SLEEP),
    ):
        _stub(bin_dir, name, body)
    if shutil.which("flock") is None:
        _stub(bin_dir, "flock", FAKE_FLOCK)
    return tools


@pytest.fixture
def server(tmp_path: Path, tools: Path) -> Server:
    return Server(tmp_path, tools)


# AC 1: an approved release runs and shows `success`.


def test_a_queued_release_is_deployed_and_reported_success(server: Server) -> None:
    result = server.run()

    assert result.returncode == 0, result.stderr
    assert [(p["id"], p["state"]) for p in server.posted()] == [
        ("7", "in_progress"),
        ("7", "success"),
    ]
    assert server.serving() == f"{IMAGE}@{NEW}"
    assert server.recorded_image() == f"{IMAGE}@{NEW}"
    assert f"{IMAGE}@{NEW}" in result.stdout


def test_steps_run_in_the_adr_order(server: Server) -> None:
    assert server.run().returncode == 0

    steps = [s for s in server.steps() if s != "read"]
    assert steps == [
        "verify",
        "status:in_progress",
        "pull",
        "up:alloy",
        "up:db",
        "backup.sh",
        "migrate",
        "monitor-password",
        "up:app+caddy",
        "health",
        "status:success",
    ]


def test_verifies_provenance_against_release_yml(server: Server) -> None:
    assert server.run().returncode == 0

    (verify,) = [c for c in server.calls("gh") if c["args"][0] == "attestation"]
    assert verify["args"] == [
        "attestation",
        "verify",
        f"oci://{IMAGE}@{NEW}",
        "-R",
        REPO,
        "--signer-workflow",
        f"{REPO}/.github/workflows/release.yml",
        "--source-ref",
        "refs/tags/v0.2.0",
        "--deny-self-hosted-runners",
    ]


@pytest.mark.parametrize(
    "ref", ["main", "refs/heads/v0.2.0", "feature/v1", "v", "vx.1", "v0.2.0;id", "v0.2.0 x", ""]
)
def test_a_deployment_not_made_from_a_release_tag_is_refused(server: Server, ref: str) -> None:
    """Only the owner can create a `v*` tag. A branch run of an edited release.yml can attest an
    image and create a deployment as github-actions[bot], but not from a tag."""
    server.publish([_deployment(7, ref=ref)], {"7": ["queued"]})

    result = server.run()

    assert result.returncode != 0
    assert [s for s in server.steps() if s != "read"] == ["status:failure"]
    assert "tag" in server.posted()[0]["description"]


def test_a_queued_status_posted_by_anyone_else_is_ignored(server: Server) -> None:
    """Anyone with Deployments write could re-queue a release that failed; only the release
    job's own `queued` starts a deploy."""
    server.publish([_deployment(7)], {"7": ["queued"]}, status_creator=OWNER)

    result = server.run()

    assert result.returncode == 0
    assert set(server.steps()) <= {"read"}


def test_pulls_and_runs_the_image_by_digest(server: Server) -> None:
    assert server.run().returncode == 0

    docker = server.calls("docker")
    assert docker[0]["args"] == ["pull", f"{IMAGE}@{NEW}"]
    for call in docker[1:]:
        assert call["app_image"] == f"{IMAGE}@{NEW}"
        assert call["args"][:5] == [
            "compose",
            "--project-directory",
            str(server.root),
            "-f",
            str(server.root / "compose.prod.yaml"),
        ]


def test_the_app_starts_without_rerunning_migrate(server: Server) -> None:
    assert server.run().returncode == 0

    (up,) = [c for c in server.calls("docker") if "up" in c["args"] and "app" in c["args"]]
    assert {"-d", "--wait", "--no-deps"} <= set(up["args"])


def test_every_wait_is_bounded(server: Server) -> None:
    """A crash-looping container restarts into `starting` again and again; an unbounded --wait
    would wait for it until the unit's timeout."""
    server.env["FAKE_UNHEALTHY"] = f"{IMAGE}@{NEW}"

    server.run()

    ups = [c["args"] for c in server.calls("docker") if "--wait" in c["args"]]
    assert len(ups) == 3  # db, the new app, the rollback
    for args in ups:
        assert args[args.index("--wait-timeout") + 1] == "120"


def test_a_healthy_release_never_goes_through_cloudflare(server: Server) -> None:
    """Bot Fight Mode challenges the host's own requests to the site (ADR-0023, amended
    2026-10-28), so the check never leaves the stack."""
    result = server.run()

    assert result.returncode == 0, result.stderr
    assert server.posted()[-1]["state"] == "success"
    assert server.calls("curl") == []
    for call in server.calls():
        assert not any("https://" in arg for arg in call["args"]), call


def test_health_is_polled_from_the_caddy_container(server: Server) -> None:
    assert server.run().returncode == 0

    (poll,) = [c for c in server.calls("docker") if "exec" in c["args"] and "wget" in c["args"]]
    assert poll["args"][:5] == [
        "compose",
        "--project-directory",
        str(server.root),
        "-f",
        str(server.root / "compose.prod.yaml"),
    ]
    assert poll["args"][5:] == [
        "exec",
        "-T",
        "caddy",
        "wget",
        "-qO-",
        "-T",
        "10",
        "--header",
        f"Host: {SITE_HOST}",
        "http://app:8000/health",
    ]


def test_each_poll_names_the_image_being_checked(server: Server) -> None:
    """compose.prod.yaml requires APP_IMAGE for every command, `exec` included."""
    server.env["FAKE_HEALTH_UNHEALTHY"] = f"{IMAGE}@{NEW}"

    server.run()

    polls = [c for c in server.calls("docker") if "exec" in c["args"] and "wget" in c["args"]]
    assert [c["app_image"] for c in polls] == [f"{IMAGE}@{NEW}"] * 30 + [f"{IMAGE}@{OLD}"]


def test_an_answer_without_the_database_counts_as_unhealthy(server: Server) -> None:
    """/health answers 200 with `"database": "unavailable"` when Postgres can't be reached."""
    server.env["FAKE_DB_UNAVAILABLE"] = f"{IMAGE}@{NEW}"

    result = server.run()

    assert result.returncode != 0
    assert server.serving() == f"{IMAGE}@{OLD}"
    assert [p["state"] for p in server.posted()] == ["in_progress", "failure"]


def test_a_slow_start_is_polled_until_healthy(server: Server) -> None:
    # Unhealthy to the poll for the new image: every poll fails, so the run must poll more
    # than once before giving up.
    server.env["FAKE_HEALTH_UNHEALTHY"] = f"{IMAGE}@{NEW}"

    server.run()

    new_image_polls = server.steps()[: server.steps().index("status:failure")].count("health")
    assert new_image_polls >= 30


# AC 2: a digest failing attestation is never pulled, and its status says why.


def test_a_digest_failing_attestation_is_never_pulled(server: Server) -> None:
    server.env["FAKE_ATTEST_FAIL"] = "1"

    result = server.run()

    assert result.returncode != 0
    assert server.calls("docker") == []
    assert server.calls("backup.sh") == []
    (posted,) = server.posted()
    assert posted["state"] == "failure"
    assert "attestation" in posted["description"]
    # The reason is gh's own, in the status as well as the journal.
    assert "no matching attestations" in posted["description"]
    assert "no matching attestations" in result.stderr
    assert server.recorded_image() == f"{IMAGE}@{OLD}"


def test_a_verification_that_fails_only_briefly_is_retried(server: Server) -> None:
    """A network blip mustn't end an approved release: a failed status is final."""
    server.env["FAKE_ATTEST_FAIL_TIMES"] = "2"

    assert server.run().returncode == 0

    assert server.steps().count("verify") == 3
    assert server.posted()[-1]["state"] == "success"


def test_a_verification_that_keeps_failing_is_tried_three_times(server: Server) -> None:
    server.env["FAKE_ATTEST_FAIL"] = "1"

    server.run()

    assert server.steps().count("verify") == 3


@pytest.mark.parametrize("digest", ["latest", "sha256:abc", "sha256:" + "A" * 64, ""])
def test_a_payload_that_is_not_a_digest_is_refused(server: Server, digest: str) -> None:
    server.publish([_deployment(7, digest=digest)], {"7": ["queued"]})

    result = server.run()

    assert result.returncode != 0
    assert [s for s in server.steps() if s != "read"] == ["status:failure"]
    assert "digest" in server.posted()[0]["description"]


def test_status_descriptions_fit_the_apis_limit(server: Server) -> None:
    server.env["FAKE_UNHEALTHY"] = f"{IMAGE}@{NEW}"

    server.run()

    assert all(len(p.get("description", "")) <= 140 for p in server.posted())


# AC 3: a deployment created by anyone but github-actions[bot] is ignored.


@pytest.mark.parametrize(
    "creator",
    [
        OWNER,
        {"login": "github-actions[bot]", "type": "User"},
        {"login": "dependabot[bot]", "type": "Bot"},
    ],
)
def test_a_deployment_not_created_by_the_release_workflow_is_ignored(
    server: Server, creator: dict[str, str]
) -> None:
    server.publish([_deployment(9, creator=creator)], {"9": ["queued"]})

    result = server.run()

    assert result.returncode == 0
    assert set(server.steps()) <= {"read"}
    assert server.posted() == []


def test_a_newer_deployment_by_someone_else_does_not_hide_the_release(server: Server) -> None:
    server.publish(
        [_deployment(9, digest=OLD, creator=OWNER), _deployment(7)],
        {"9": ["queued"], "7": ["queued"]},
    )

    assert server.run().returncode == 0

    assert [(p["id"], p["state"]) for p in server.posted()] == [
        ("7", "in_progress"),
        ("7", "success"),
    ]
    assert server.serving() == f"{IMAGE}@{NEW}"


# Only the newest release counts, so an older approval never rolls production back.


def test_an_older_queued_release_is_never_deployed(server: Server) -> None:
    server.publish(
        [_deployment(8, digest=OLD), _deployment(7)],
        {"8": ["success", "in_progress", "queued"], "7": ["queued"]},
    )

    result = server.run()

    assert result.returncode == 0
    assert set(server.steps()) <= {"read"}


@pytest.mark.parametrize("latest", ["success", "failure", "in_progress", "inactive", "error"])
def test_nothing_happens_unless_the_newest_release_is_queued(server: Server, latest: str) -> None:
    server.publish([_deployment(7)], {"7": [latest, "queued"]})

    result = server.run()

    assert result.returncode == 0
    assert set(server.steps()) <= {"read"}
    assert result.stdout == ""


def test_nothing_happens_without_any_deployment(server: Server) -> None:
    server.publish([], {})

    result = server.run()

    assert result.returncode == 0
    assert set(server.steps()) <= {"read"}


# AC 4: when /health fails, the previous digest serves again.


@pytest.mark.parametrize("seen_by", ["FAKE_UNHEALTHY", "FAKE_HEALTH_UNHEALTHY"])
def test_a_failing_health_check_restores_the_previous_digest(server: Server, seen_by: str) -> None:
    server.env[seen_by] = f"{IMAGE}@{NEW}"

    result = server.run()

    assert result.returncode != 0
    assert server.serving() == f"{IMAGE}@{OLD}"
    assert server.recorded_image() == f"{IMAGE}@{OLD}"
    (rollback,) = [
        c
        for c in server.calls("docker")
        if c["app_image"] == f"{IMAGE}@{OLD}" and "up" in c["args"]
    ]
    # The previous image's alembic doesn't know the new head, so migrate must not run with it.
    assert {"-d", "--wait", "--no-deps", "app"} <= set(rollback["args"])
    assert "caddy" not in rollback["args"]
    assert [p["state"] for p in server.posted()] == ["in_progress", "failure"]
    description = server.posted()[-1]["description"]
    assert "health" in description
    assert "rolled back" in description
    assert OLD[:19] in description


def test_the_database_is_never_rolled_back(server: Server) -> None:
    server.env["FAKE_UNHEALTHY"] = f"{IMAGE}@{NEW}"

    server.run()

    after_migrate = server.steps()[server.steps().index("migrate") + 1 :]
    assert "migrate" not in after_migrate
    assert "backup.sh" not in after_migrate
    for call in server.calls("docker"):
        assert not {"down", "rm", "restore"} & set(call["args"])


def test_a_rollback_that_is_unhealthy_too_says_so(server: Server) -> None:
    server.env["FAKE_UNHEALTHY"] = f"{IMAGE}@{NEW},{IMAGE}@{OLD}"

    result = server.run()

    assert result.returncode != 0
    assert "unhealthy too" in server.posted()[-1]["description"]
    assert server.recorded_image() == f"{IMAGE}@{OLD}"


@pytest.mark.parametrize("seen_by", ["FAKE_UNHEALTHY", "FAKE_HEALTH_UNHEALTHY"])
def test_a_failing_first_deploy_has_nothing_to_roll_back_to(server: Server, seen_by: str) -> None:
    (server.root / "app-image").unlink()
    (server.root / "app-version").unlink()
    server.env[seen_by] = f"{IMAGE}@{NEW}"

    result = server.run()

    assert result.returncode != 0
    assert "no previous release" in server.posted()[-1]["description"]
    assert not (server.root / "app-image").exists()
    assert not (server.root / "app-version").exists()


# #293: the app reports the release tag as `service.version` (ADR-0030).


def test_the_app_starts_with_the_release_tag_and_records_it(server: Server) -> None:
    result = server.run()

    assert result.returncode == 0, result.stderr
    assert [c["release_tag"] for c in server.app_starts()] == ["v0.2.0"]
    assert server.recorded_version() == "v0.2.0"


def test_a_rollback_runs_with_the_previous_releases_tag(server: Server) -> None:
    server.env["FAKE_UNHEALTHY"] = f"{IMAGE}@{NEW}"

    server.run()

    assert [(c["app_image"], c["release_tag"]) for c in server.app_starts()] == [
        (f"{IMAGE}@{NEW}", "v0.2.0"),
        (f"{IMAGE}@{OLD}", "v0.1.0"),
    ]
    assert server.recorded_version() == "v0.1.0"


def test_a_rollback_to_a_release_without_a_recorded_tag_reports_unknown(server: Server) -> None:
    (server.root / "app-version").unlink()
    server.env["FAKE_UNHEALTHY"] = f"{IMAGE}@{NEW}"

    server.run()

    assert server.app_starts()[-1]["release_tag"] == "unknown"


def test_a_failed_pull_changes_nothing(server: Server) -> None:
    server.env["FAKE_PULL_FAIL"] = "1"

    result = server.run()

    assert result.returncode != 0
    assert "pull" in server.posted()[-1]["description"]
    assert server.calls("backup.sh") == []
    assert [s for s in server.steps() if s.startswith("up")] == []


def _stop_while_hanging(server: Server) -> int:
    """Runs the script until a step hangs, then stops it as systemd's TimeoutStartSec= does:
    SIGTERM to the unit's whole control group."""
    process = subprocess.Popen(
        [str(server.script)],
        env=server.env,
        cwd=server.root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not (server.state / "hanging").exists():
            assert time.monotonic() < deadline, "no step hung"
            time.sleep(0.05)

        os.killpg(process.pid, signal.SIGTERM)
        return process.wait(timeout=20)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_a_run_stopped_by_its_timeout_reports_failure(server: Server) -> None:
    """Only a `queued` deployment is ever picked up again, so one left `in_progress` would
    never be retried or reported."""
    server.env["FAKE_HANG"] = "pull"

    assert _stop_while_hanging(server) != 0

    assert [p["state"] for p in server.posted()] == ["in_progress", "failure"]
    assert "stopped" in server.posted()[-1]["description"]
    assert server.recorded_image() == f"{IMAGE}@{OLD}"
    assert not (server.state / "serving").exists()


def test_a_run_stopped_once_the_new_app_started_rolls_back(server: Server) -> None:
    """A crash-looping release can hold `up --wait` until the unit's timeout."""
    server.env["FAKE_HANG"] = "app"
    server.env["FAKE_HANG_DIGEST"] = NEW

    assert _stop_while_hanging(server) != 0

    assert server.serving() == f"{IMAGE}@{OLD}"
    assert [p["state"] for p in server.posted()] == ["in_progress", "failure"]
    assert "rolled back" in server.posted()[-1]["description"]
    assert server.recorded_image() == f"{IMAGE}@{OLD}"
    assert server.app_starts()[-1]["release_tag"] == "v0.1.0"
    assert server.recorded_version() == "v0.1.0"


def test_a_failed_read_of_the_deployments_fails_the_unit(server: Server) -> None:
    (server.state / "deployments.json").write_text("not json")

    result = server.run()

    assert result.returncode != 0
    assert server.posted() == []
    assert server.calls("docker") == []


# AC 5: every run that migrates leaves a backup taken before it.


def test_the_backup_runs_with_the_predeploy_kind_before_migrate(server: Server) -> None:
    assert server.run().returncode == 0

    (backup,) = server.calls("backup.sh")
    assert backup["args"] == ["predeploy"]
    steps = server.steps()
    assert steps.index("up:db") < steps.index("backup.sh") < steps.index("migrate")


def test_a_failed_backup_never_migrates(server: Server) -> None:
    server.env["FAKE_BACKUP_FAIL"] = "1"

    result = server.run()

    assert result.returncode != 0
    assert "migrate" not in server.steps()
    assert "up:app+caddy" not in server.steps()
    assert "backup" in server.posted()[-1]["description"]
    assert server.recorded_image() == f"{IMAGE}@{OLD}"


def test_a_failed_migration_leaves_the_app_as_it_was(server: Server) -> None:
    server.env["FAKE_MIGRATE_FAIL"] = "1"

    result = server.run()

    assert result.returncode != 0
    assert "up:app+caddy" not in server.steps()
    assert "migration" in server.posted()[-1]["description"]
    assert "relation already exists" in result.stderr
    assert server.recorded_image() == f"{IMAGE}@{OLD}"


# AC 6: a run that starts during another does nothing, and no log line holds the token.


@pytest.fixture
def lock_held(server: Server) -> Iterator[None]:
    with (server.root / ".deploy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@pytest.mark.usefixtures("lock_held")
def test_a_run_that_starts_during_another_does_nothing(server: Server) -> None:
    result = server.run()

    assert result.returncode == 0
    assert server.calls() == []
    assert "another deploy is running" in result.stdout


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "FAKE_ATTEST_FAIL",
        "FAKE_PULL_FAIL",
        "FAKE_BACKUP_FAIL",
        "FAKE_MIGRATE_FAIL",
        "FAKE_UNHEALTHY",
    ],
)
def test_the_token_reaches_only_gh_and_never_the_output(
    server: Server, failure: str | None
) -> None:
    if failure:
        server.env[failure] = f"{IMAGE}@{NEW}" if failure == "FAKE_UNHEALTHY" else "1"

    result = server.run()

    assert TOKEN not in result.stdout + result.stderr
    assert all(c["gh_token_is_the_token"] for c in server.calls("gh"))
    assert not any(c["token_elsewhere"] for c in server.calls())
    for c in server.calls():
        if c["tool"] != "gh":
            assert not c["gh_token_is_the_token"]


def test_refuses_to_run_without_the_token_credential(server: Server) -> None:
    del server.env["CREDENTIALS_DIRECTORY"]

    result = server.run()

    assert result.returncode == 2
    assert "github-token" in result.stderr
    assert server.calls() == []


def test_refuses_to_run_without_the_site_host(server: Server) -> None:
    del server.env["SITE_HOST"]

    result = server.run()

    assert result.returncode == 2
    assert "SITE_HOST" in result.stderr
    assert server.calls() == []


def test_alloy_starts_without_waiting_and_by_itself(server: Server) -> None:
    """ADR-0030 invariant 5: telemetry never holds up a deploy."""
    assert server.run().returncode == 0

    (up,) = [c for c in server.calls("docker") if "up" in c["args"] and "alloy" in c["args"]]
    assert {"-d", "--no-deps"} <= set(up["args"])
    assert "--wait" not in up["args"]
    assert up["args"][-1] == "alloy"


def test_an_alloy_that_fails_to_start_never_fails_the_deploy(server: Server) -> None:
    server.env["FAKE_ALLOY_FAIL"] = "1"

    result = server.run()

    assert result.returncode == 0, result.stderr
    assert server.posted()[-1]["state"] == "success"
    assert server.serving() == f"{IMAGE}@{NEW}"
    assert "alloy" in result.stderr


# ADR-0030 (#274): the last line of a run that did something is its outcome, shipped to Grafana
# Cloud from the journal. An idle poll prints nothing.


def _last(text: str) -> str:
    return text.rstrip("\n").splitlines()[-1]


def test_a_deployed_release_ends_with_the_ok_outcome_line(server: Server) -> None:
    result = server.run()

    assert result.returncode == 0, result.stderr
    assert _last(result.stdout) == f"deploy: result=ok deployment=7 image={NEW[:19]}"


@pytest.mark.parametrize(
    "failure",
    ["FAKE_PULL_FAIL", "FAKE_MIGRATE_FAIL", "FAKE_UNHEALTHY", "FAKE_ATTEST_FAIL"],
)
def test_a_forced_failure_ends_with_the_failed_outcome_line_and_a_non_zero_exit(
    server: Server, failure: str
) -> None:
    server.env[failure] = f"{IMAGE}@{NEW}" if failure == "FAKE_UNHEALTHY" else "1"
    if failure == "FAKE_ATTEST_FAIL":
        server.env["FAKE_ATTEST_FAIL_TIMES"] = "9"

    result = server.run()

    assert result.returncode == 1
    assert _last(result.stderr) == f"deploy: result=failed deployment=7 image={NEW[:19]}"
    assert "result=ok" not in result.stdout


def test_a_run_stopped_by_its_timeout_ends_with_the_failed_outcome_line(server: Server) -> None:
    server.env["FAKE_HANG"] = "pull"
    process = subprocess.Popen(
        [str(server.script)],
        env=server.env,
        cwd=server.root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not (server.state / "hanging").exists():
            assert time.monotonic() < deadline, "no step hung"
            time.sleep(0.05)
        os.killpg(process.pid, signal.SIGTERM)
        _, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    assert _last(stderr) == f"deploy: result=failed deployment=7 image={NEW[:19]}"


def test_a_refused_start_ends_with_the_failed_outcome_line(server: Server) -> None:
    del server.env["SITE_HOST"]

    result = server.run()

    assert result.returncode == 2
    assert _last(result.stderr) == "deploy: result=failed reason=config exit=2"


def test_a_failed_read_of_the_deployments_ends_with_the_failed_outcome_line(
    server: Server,
) -> None:
    (server.state / "deployments.json").write_text("not json")

    result = server.run()

    assert result.returncode != 0
    assert _last(result.stderr).startswith("deploy: result=failed reason=poll exit=")


def test_an_idle_poll_prints_no_outcome_line(server: Server) -> None:
    server.publish([_deployment(7)], {"7": ["success"]})

    result = server.run()

    assert result.returncode == 0
    assert "result=" not in result.stdout + result.stderr


def test_the_outcome_line_carries_no_token(server: Server) -> None:
    server.env["FAKE_PULL_FAIL"] = "1"

    result = server.run()

    assert TOKEN not in _last(result.stderr)


@pytest.mark.parametrize("failure", ["FAKE_PULL_FAIL", "FAKE_UNHEALTHY"])
def test_a_status_github_refuses_does_not_hide_the_failed_outcome_line(
    server: Server, failure: str
) -> None:
    """A failing network is a likely cause of the failed deploy, and of the failed report too."""
    server.env[failure] = f"{IMAGE}@{NEW}" if failure == "FAKE_UNHEALTHY" else "1"
    server.env["FAKE_GH_POST_FAIL"] = "failure"

    result = server.run()

    assert result.returncode == 1
    assert _last(result.stderr) == f"deploy: result=failed deployment=7 image={NEW[:19]}"


def test_a_success_github_cannot_be_told_of_still_ends_with_the_ok_outcome_line(
    server: Server,
) -> None:
    server.env["FAKE_GH_POST_FAIL"] = "success"

    result = server.run()

    assert result.returncode == 0
    assert _last(result.stdout) == f"deploy: result=ok deployment=7 image={NEW[:19]}"


# ADR-0030 (#273): the monitor role the migration creates gets its password from alloy.env.


def test_the_monitor_password_is_set_after_migrate_in_the_db_container(server: Server) -> None:
    result = server.run()

    assert result.returncode == 0, result.stderr
    (call,) = [c for c in server.calls("docker") if "psql" in c["args"]]
    assert call["args"][: call["args"].index("exec")] == [
        "compose",
        "--project-directory",
        str(server.root),
        "-f",
        f"{server.root}/compose.prod.yaml",
    ]
    assert call["app_image"] == f"{IMAGE}@{NEW}"
    assert f"PASSWORD '{MONITOR_PASSWORD}'" in (server.state / "psql-stdin").read_text()
    assert MONITOR_PASSWORD not in result.stdout + result.stderr
    # This unit's journal is shipped as `job="deploy"`: only `deploy: result=` lines end a run.
    assert "monitor-password: result=" not in result.stdout


@pytest.mark.parametrize("failure", ["psql", "no alloy.env"])
def test_a_monitor_password_that_cannot_be_set_never_fails_the_deploy(
    server: Server, failure: str
) -> None:
    """ADR-0030 invariant 5: Postgres metrics stay off until it is set, and the release ships."""
    if failure == "psql":
        server.env["FAKE_MONITOR_FAIL"] = "1"
    else:
        (server.root / "alloy.env").unlink()

    result = server.run()

    assert result.returncode == 0, result.stderr
    assert server.posted()[-1]["state"] == "success"
    assert "Postgres metrics are off" in result.stderr
    assert _last(result.stdout) == f"deploy: result=ok deployment=7 image={NEW[:19]}"
