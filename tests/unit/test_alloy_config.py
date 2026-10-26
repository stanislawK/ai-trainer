"""`deploy/alloy/config.alloy` keeps ADR-0030's relay rules (ticket #272): the app's OTLP and
this stack's container logs go to Grafana Cloud, labelled by compose service only, with
email-shaped values masked, and every credential read from `alloy.env`.

`make alloy-check` runs Alloy's own `fmt` and `validate` on the file; these tests pin what the
validator can't know."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = (ROOT / "deploy/alloy/config.alloy").read_text()
ENV_EXAMPLE = ROOT / "deploy/alloy.env.example"
# The container-log pipeline and the journal pipeline (#274) are checked apart: each has its own
# bounded label.
CONTAINER_LOGS, JOURNAL = (
    CONFIG.split('loki.relabel "jobs"', 1)[0],
    CONFIG.split('loki.relabel "jobs"', 1)[1].split('loki.process "scrub"', 1)[0],
)


def _alloy_string(name: str, text: str = CONFIG) -> list[str]:
    """Every `<name> = "<value>"` in the config, decoded like an Alloy (Go) string literal."""
    return [
        json.loads(literal) for literal in re.findall(rf'\b{name}\s*=\s*("(?:[^"\\]|\\.)*")', text)
    ]


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "compose.prod.yaml").read_text())
    return loaded


def test_reads_only_this_stacks_containers(compose: dict[str, Any]) -> None:
    """Anything else on the host, the `make start` stack on a laptop included, stays out."""
    assert f'"com.docker.compose.project={compose["name"]}"' in CONFIG


def test_logs_are_labelled_by_compose_service_and_nothing_else() -> None:
    """ADR-0030 invariant 4: bounded labels only; request and user IDs stay in the line."""
    assert _alloy_string("target_label", CONTAINER_LOGS) == ["service"]
    (source,) = re.findall(r"source_labels\s*=\s*\[([^\]]*)\]", CONTAINER_LOGS)
    assert json.loads(f"[{source}]") == ["__meta_docker_container_label_com_docker_compose_service"]
    for unbounded in ("request_id", "user_id", "trace_id", "span_id"):
        assert unbounded not in CONFIG


def test_the_otlp_receiver_listens_where_the_app_sends(compose: dict[str, Any]) -> None:
    endpoint = compose["services"]["app"]["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert endpoint.endswith(":4318")
    assert _alloy_string("endpoint", CONFIG.split("otelcol.receiver.otlp", 1)[1])[0] == (
        "0.0.0.0:4318"
    )


def test_every_credential_comes_from_alloy_env() -> None:
    used = set(re.findall(r'sys\.env\("([A-Z0-9_]+)"\)', CONFIG))
    listed = {
        line.split("=", 1)[0]
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert used == listed
    assert used >= {"GRAFANA_CLOUD_TOKEN", "GRAFANA_CLOUD_OTLP_URL", "GRAFANA_CLOUD_LOGS_URL"}
    # No URL, user or token is written into the file itself.
    assert "grafana.net" not in CONFIG
    assert not re.search(r"(username|password)\s*=\s*\"", CONFIG)


def _masked(line: str) -> str:
    """`stage.replace` replaces each capture group of its expression with `replace`.

    RE2's Unicode classes are spelled out for Python's `re`, which has none: `\\pL\\pN` (a letter
    or digit, inside a class that already holds `_`) is `\\w`, and `\\pL` alone `[^\\W\\d_]`.
    `make alloy-check` compiles the real expression with RE2.
    """
    (expression,) = _alloy_string("expression")
    (replace,) = _alloy_string("replace")
    python = expression.replace(r"\pL\pN", r"\w").replace(r"\pL", r"[^\W\d_]")
    return re.sub(python, lambda m: m.group(0).replace(m.group(1), replace), line)


@pytest.mark.parametrize(
    ("line", "masked"),
    [
        (
            "DETAIL:  Key (email)=(ada.lovelace+climb@example.co.uk) already exists.",
            "DETAIL:  Key (email)=(<email>) already exists.",
        ),
        ("sign-in for Ada@Example.COM refused", "sign-in for <email> refused"),
        ("a@b.io and c_d@e-f.org", "<email> and <email>"),
        # Europe first (PRD 0008 G15): names and domains outside ASCII.
        ('Key (email)=("józef.nowak@example.pl")', 'Key (email)=("<email>")'),
        ("user@bücher.de and Łucja@poczta.pl", "<email> and <email>"),
        ("GET /sign-in?email=carol%40example.net", "GET /sign-in?email=<email>"),
        ('{"logger": "ai_trainer.access", "route": "/sign-in"}', None),
        ("postgres@db:5432 ready to accept connections", None),
        ("2026-10-25T18:00:00Z GET /messages/{message_id}/reply 200 12.0ms", None),
    ],
)
def test_email_shaped_values_are_masked_before_shipping(line: str, masked: str | None) -> None:
    assert _masked(line) == (line if masked is None else masked)


def test_alloy_check_uses_the_image_compose_runs(compose: dict[str, Any]) -> None:
    makefile = (ROOT / "Makefile").read_text()
    target = makefile.split("\nalloy-check:", 1)[1].split("\n\n", 1)[0]
    image = compose["services"]["alloy"]["image"]
    assert "@sha256:" in image
    assert f"\nALLOY_IMAGE = {image}\n" in makefile
    assert target.count("$(ALLOY_IMAGE)") == 3
    assert "fmt --test" in target
    assert "validate" in target


# ADR-0030 (#274): only the three scheduled jobs' lines leave the host's journal.

JOB_IDENTIFIERS = ["ai-trainer-backup", "ai-trainer-purge", "ai-trainer-deploy"]
# What the host journal holds, by SYSLOG_IDENTIFIER: the three jobs, and everything else that
# could look like them.
FIXTURE_JOURNAL = [
    ("ai-trainer-backup", "backup: result=ok kind=nightly", "backup"),
    ("ai-trainer-purge", "purge: result=ok deleted=0", "purge"),
    ("ai-trainer-deploy", "deploy: result=failed deployment=7 image=sha256:aaaaaaaaaaaa", "deploy"),
    ("sshd", "Accepted publickey for deploy from 203.0.113.9", None),
    ("sudo", "deploy : COMMAND=/usr/bin/docker ps", None),
    ("dockerd", 'level=info msg="Container start"', None),
    ("systemd", "Started ai-trainer-backup.service.", None),
    ("ai-trainer-backup-extra", "backup: result=ok", None),
    ("my-ai-trainer-purge", "purge: result=ok deleted=0", None),
    ("ai-trainer-restore-drill", "drill: result=ok", None),
    ("", "a line with no identifier", None),
]


def _shipped(identifier: str) -> str | None:
    """The `job` label a journal entry gets, or None when the `keep` rule drops it. This pins the
    relabel semantics as Alloy documents them; the host check in RUNBOOK 11.5 is the proof.

    Alloy's relabel regex is anchored at both ends, and `$1` in `replacement` is its first group.
    """
    (keep, label) = CONFIG.split('loki.relabel "jobs"', 1)[1].split("rule {")[1:]
    regexes = {_alloy_string("regex", rule)[0] for rule in (keep, label)}
    assert len(regexes) == 1, "the keep rule and the job label must select the same identifiers"
    match = re.fullmatch(regexes.pop(), identifier)
    if match is None:
        return None
    assert _alloy_string("action", keep) == ["keep"]
    assert _alloy_string("replacement", label) == ["$1"]
    return match.group(1)


@pytest.mark.parametrize(("identifier", "line", "job"), FIXTURE_JOURNAL)
def test_only_the_three_jobs_journal_lines_are_shipped(
    identifier: str, line: str, job: str | None
) -> None:
    assert _shipped(identifier) == job


def test_the_journal_rules_read_the_syslog_identifier_the_units_set() -> None:
    sources = re.findall(r"source_labels\s*=\s*\[([^\]]*)\]", JOURNAL)
    assert {json.loads(f"[{source}]")[0] for source in sources} == {"__journal_syslog_identifier"}
    for name in JOB_IDENTIFIERS:
        unit = (ROOT / f"deploy/systemd/{name}.service").read_text()
        assert f"SyslogIdentifier={name}\n" in unit
        assert _shipped(name) is not None


def test_the_journal_is_labelled_by_job_and_nothing_else() -> None:
    assert _alloy_string("target_label", JOURNAL) == ["job"]
    # No fixed `labels = {…}` on the source either: `job` is the only label a line gets.
    assert not re.search(r"^\s*labels\s*=", JOURNAL, re.MULTILINE)


def test_the_relabel_rules_are_attached_to_the_journal_source() -> None:
    """Without this line every entry on the host, sshd and sudo included, would be shipped."""
    source = JOURNAL.split('loki.source.journal "jobs"', 1)[1]
    assert re.search(r"relabel_rules\s*=\s*loki\.relabel\.jobs\.rules", source)


def test_the_journal_lines_pass_the_email_scrub() -> None:
    source = JOURNAL.split('loki.source.journal "jobs"', 1)[1]
    assert re.search(r"forward_to\s*=\s*\[loki\.process\.scrub\.receiver\]", source)


def test_alloy_reads_the_host_journal_mounted_read_only(compose: dict[str, Any]) -> None:
    volumes = compose["services"]["alloy"]["volumes"]
    for path in ("/var/log/journal", "/run/log/journal", "/etc/machine-id"):
        assert f"{path}:{path}:ro" in volumes
    # No `path`: Alloy then reads both mounted directories.
    assert _alloy_string("path", JOURNAL) == []


def test_the_outcome_lines_start_with_the_job_label() -> None:
    """Dashboards and alerts (G13) match `<job>: result=` under the `job` label."""
    for job, script in (
        ("backup", "deploy/backup.sh"),
        ("deploy", "deploy/deploy.sh"),
        ("purge", "src/ai_trainer/cli.py"),
    ):
        assert f"{job}: result=ok" in (ROOT / script).read_text()
