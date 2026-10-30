"""`deploy/grafana/` alerts the owner on an outage or a failed job (ADR-0030, ticket #277).

`alerts.json` is one Grafana-managed rule group, the body of the provisioning API's rule-group
PUT, which `deploy/grafana/import-alerts.sh` sends with the stack's own datasource UIDs.
`synthetic-health.json` is the Synthetic Monitoring check behind the site-down rule, and
`smoke-probe.alloy` its stand-in on the smoke run, where no Synthetic Monitoring exists.

A rule that queries a series nothing sends never fires, and nothing says so. So every metric,
label, label value and line filter a rule names is checked against what the stack emits
(`grafana_catalogue`): the app's instruments, Alloy's keep list, the probe and the jobs' outcome
lines."""

import json
import re
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import yaml

from tests.ops.grafana_catalogue import (
    CHECK,
    GRAFANA,
    alloy_string,
    check_loki_query,
    check_prometheus_query,
)

GROUP: dict[str, Any] = json.loads((GRAFANA / "alerts.json").read_text())
RULES: dict[str, dict[str, Any]] = {rule["uid"]: rule for rule in GROUP["rules"]}
PROBE = (GRAFANA / "smoke-probe.alloy").read_text()
IMPORT = GRAFANA / "import-alerts.sh"

PROMETHEUS_UID = "__PROMETHEUS_UID__"
LOKI_UID = "__LOKI_UID__"


def _queries(rule: dict[str, Any]) -> list[tuple[str, str]]:
    """(datasource UID, expression) of each query a rule runs, its expressions left out."""
    return [
        (query["datasourceUid"], query["model"]["expr"])
        for query in rule["data"]
        if query["datasourceUid"] != "__expr__"
    ]


PROMETHEUS_QUERIES = [
    (uid, expr)
    for uid, rule in RULES.items()
    for ds, expr in _queries(rule)
    if ds == PROMETHEUS_UID
]
LOKI_QUERIES = [
    (uid, expr) for uid, rule in RULES.items() for ds, expr in _queries(rule) if ds == LOKI_UID
]


@pytest.mark.parametrize(("uid", "expr"), PROMETHEUS_QUERIES)
def test_each_prometheus_query_names_only_emitted_series_labels_and_values(
    uid: str, expr: str
) -> None:
    check_prometheus_query(uid, expr)


@pytest.mark.parametrize(("uid", "expr"), LOKI_QUERIES)
def test_each_loki_query_selects_shipped_streams_and_filters_on_printed_lines(
    uid: str, expr: str
) -> None:
    """A filter picks one outcome: every line of it that each job prints, and none of the other."""
    check_loki_query(uid, expr, one_outcome=True)


_COUNT = "ai_trainer_llm_call_duration_seconds_count"
_REQUESTS = "http_server_request_duration_seconds_count"
_BACKUP = 'count_over_time({job="backup"} |= '
REJECTED_PROMETHEUS = {
    'sum(rate(http_server_requests_total{job="ai-trainer"}[5m]))': "nothing emits",
    f'sum(rate({_REQUESTS}{{status="500"}}[5m]))': "carries 'status'",
    f'sum(rate({_COUNT}{{outcome="failed"}}[5m]))': "outcome",
    f'sum(rate({_COUNT}{{outcome!~"success|error|timeout|cancelled"}}[5m]))': "excludes every",
    f"sum(rate(up[5m])) / sum(rate({_REQUESTS}{{}}[5m]))": "bare metric",
}
REJECTED_LOKI = {
    _BACKUP + '"backup: result=success" [1h])': "no backup line contains",
    _BACKUP + '"Grafana Cloud" [1h])': "no backup line contains",
    _BACKUP + '"backup: result=ok kind=<kind>" [1h])': "no backup line contains",
    _BACKUP + '"reason=killed" [1h])': "misses",
    'count_over_time({job=~"backup|purge"} |= "result=" [1h])': "matches both ok and failed",
    'count_over_time({job="backups"} [1h])': "emitted",
    'count_over_time({job="backup", service="app"} [1h])': "no stream carries",
    'count_over_time({service!~".+"} [15m])': "excludes every value",
}


@pytest.mark.parametrize(("expr", "reason"), REJECTED_PROMETHEUS.items())
def test_the_catalogue_rejects_a_prometheus_query_nothing_answers(expr: str, reason: str) -> None:
    with pytest.raises(AssertionError, match=reason):
        test_each_prometheus_query_names_only_emitted_series_labels_and_values("x", expr)


@pytest.mark.parametrize(("expr", "reason"), REJECTED_LOKI.items())
def test_the_catalogue_rejects_a_loki_query_nothing_answers(expr: str, reason: str) -> None:
    """Text no job prints (a comment's placeholder included), a filter that can't tell ok from
    failed, and a selector no stream can match."""
    with pytest.raises(AssertionError, match=reason):
        test_each_loki_query_selects_shipped_streams_and_filters_on_printed_lines("x", expr)


# ---------------------------------------------------------------------------------------------
# The ticket's alert set.


def _threshold(rule: dict[str, Any]) -> tuple[str, float]:
    condition = next(q for q in rule["data"] if q["refId"] == rule["condition"])
    model = condition["model"]
    assert model["type"] == "threshold"
    assert condition["datasourceUid"] == "__expr__"
    evaluator = model["conditions"][0]["evaluator"]
    return evaluator["type"], evaluator["params"][0]


ALERTS = {
    "ai-trainer-site-down": (
        PROMETHEUS_UID, 'probe_success{job="ai-trainer-health"}', ("lt", 1), "2m", "Alerting"
    ),
    "ai-trainer-http-5xx": (PROMETHEUS_UID, '"5xx"}[10m]', ("gt", 0.05), "0s", "OK"),
    "ai-trainer-log-turn-p95": (
        PROMETHEUS_UID, "histogram_quantile(0.95,", ("gt", 10), "0s", "OK"
    ),
    "ai-trainer-model-call-errors": (
        PROMETHEUS_UID, 'outcome=~"error|timeout"}[15m]', ("gt", 0.2), "0s", "OK"
    ),
    "ai-trainer-backup-missing": (
        LOKI_UID,
        'absent_over_time({job="backup"} |= "backup: result=ok" [26h])',
        ("gt", 0),
        "0s",
        "OK",
    ),
    "ai-trainer-job-failed": (LOKI_UID, '|= "result=failed" [1h]', ("gt", 0), "0s", "OK"),
    "ai-trainer-disk": (PROMETHEUS_UID, 'mountpoint="/"', ("gt", 0.8), "10m", "OK"),
    "ai-trainer-no-telemetry": (
        PROMETHEUS_UID,
        'absent_over_time(node_load1{instance="ai-trainer-prod", job="integrations/unix"}[15m])',
        ("gt", 0),
        "0s",
        "OK",
    ),
}  # fmt: skip


@pytest.mark.parametrize(
    ("uid", "datasource", "in_expr", "threshold", "pending", "no_data"),
    [(uid, *expected) for uid, expected in ALERTS.items()],
)
def test_each_alert_of_the_ticket_has_its_query_threshold_and_window(
    uid: str,
    datasource: str,
    in_expr: str,
    threshold: tuple[str, float],
    pending: str,
    no_data: str,
) -> None:
    rule = RULES[uid]
    [(ds, expr)] = _queries(rule)
    assert ds == datasource
    assert in_expr in expr
    assert _threshold(rule) == threshold
    assert (rule["for"], rule["noDataState"], rule["execErrState"]) == (pending, no_data, "Error")


def test_the_group_holds_exactly_the_ticket_alerts_in_one_folder() -> None:
    assert list(RULES) == list(ALERTS)
    assert len(GROUP["rules"]) == 8
    assert (GROUP["title"], GROUP["folderUid"], GROUP["interval"]) == (
        "ai-trainer",
        "ai-trainer",
        60,
    )
    for rule in GROUP["rules"]:
        assert (rule["folderUID"], rule["ruleGroup"]) == ("ai-trainer", "ai-trainer")
        assert rule["annotations"]["summary"]
        assert rule["annotations"]["description"]
        assert rule["annotations"]["runbook_url"].endswith("deploy/RUNBOOK.md#117-alerts")
        assert len(rule["uid"]) <= 40
        assert re.fullmatch(r"[\w-]+", rule["uid"])


def test_a_ratio_or_quantile_over_no_traffic_is_no_data_rather_than_nan() -> None:
    """0/0 and a quantile of empty buckets are NaN; a `> 0` on the denominator turns them into
    no series, which these rules treat as OK."""
    for uid in ("ai-trainer-http-5xx", "ai-trainer-model-call-errors", "ai-trainer-log-turn-p95"):
        [(_, expr)] = _queries(RULES[uid])
        assert re.search(r"\[\d+m\]\)\) > 0\)", expr), uid


def test_failed_jobs_are_told_apart_by_job() -> None:
    [(_, expr)] = _queries(RULES["ai-trainer-job-failed"])
    assert expr.startswith("sum by (job) (")
    assert "{{ $labels.job }}" in RULES["ai-trainer-job-failed"]["annotations"]["summary"]


def test_only_placeholder_datasources_so_the_import_names_the_stacks_own() -> None:
    uids = {q["datasourceUid"] for rule in GROUP["rules"] for q in rule["data"]}
    assert uids == {PROMETHEUS_UID, LOKI_UID, "__expr__"}
    assert "grafanacloud" not in json.dumps(GROUP)


# ---------------------------------------------------------------------------------------------
# The health check and its smoke stand-in.

HEALTH_REGEX = CHECK["settings"]["http"]["failIfBodyNotMatchesRegexp"]


def test_the_check_probes_health_through_cloudflare_and_needs_the_database_ok() -> None:
    http = CHECK["settings"]["http"]
    assert CHECK["target"] == "https://__SITE_HOST__/health"
    assert CHECK["job"] == "ai-trainer-health"
    assert CHECK["enabled"] is True
    assert (CHECK["frequency"], CHECK["timeout"]) == (60000, 10000)
    assert http["validStatusCodes"] == [200]
    assert http["failIfNotSSL"] is True
    assert http["noFollowRedirects"] is True
    assert HEALTH_REGEX == ['"database":"ok"']
    assert re.search(HEALTH_REGEX[0], '{"database":"ok"}')
    assert not re.search(HEALTH_REGEX[0], '{"database":"unavailable"}')
    # Probe IDs are the stack's own; the owner fills them in (RUNBOOK 11.7).
    assert CHECK["probes"] == []
    # The site-down rule is ours, so the check's own alerts stay off.
    assert (CHECK["alertSensitivity"], CHECK["basicMetricsOnly"]) == ("none", True)


def test_the_smoke_probe_asks_what_the_check_asks_and_labels_it_alike() -> None:
    modules = yaml.safe_load(alloy_string("config", PROBE)[0])["modules"]
    http = modules["health"]["http"]
    assert modules["health"]["prober"] == "http"
    assert http["fail_if_body_not_matches_regexp"] == HEALTH_REGEX
    assert http["valid_status_codes"] == CHECK["settings"]["http"]["validStatusCodes"]
    assert http["fail_if_not_ssl"] is True
    assert http["no_follow_redirects"] is True
    # Through Caddy's 443 with the smoke run's Cloudflare stand-in certificate.
    assert http["tls_config"]["cert_file"] == "/smoke/client.pem"
    assert http["tls_config"]["ca_file"] == "/smoke/origin-ca.pem"
    assert http["headers"]["Host"] == http["tls_config"]["server_name"] == "localhost"
    assert alloy_string("address", PROBE) == ["https://host.docker.internal/health"]
    replacements = alloy_string("replacement", PROBE)
    assert CHECK["job"] in replacements
    assert "https://localhost/health" in replacements
    assert alloy_string("url", PROBE) == ["http://host.docker.internal:9090/api/v1/write"]


# ---------------------------------------------------------------------------------------------
# The import.


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: Any


@pytest.fixture
def grafana() -> Iterator[tuple[str, list[Recorded]]]:
    """A stand-in Grafana that records each request and answers the folder POST with 409 the
    second time, as Grafana does for a folder that already exists."""
    seen: list[Recorded] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            seen.append(
                Recorded(self.command, self.path, dict(self.headers), json.loads(raw or b"null"))
            )
            folders = [r for r in seen if r.path == "/api/folders"]
            status = 409 if self.path == "/api/folders" and len(folders) > 1 else 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        do_POST = do_PUT = _record  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


def _import(url: str, *args: str, token: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if token is not None:
        env["GRAFANA_TOKEN"] = token
    return subprocess.run(
        [str(IMPORT), url, *args], capture_output=True, text=True, env=env, timeout=30, check=False
    )


def test_the_import_creates_the_folder_then_puts_the_group_with_the_stacks_uids(
    grafana: tuple[str, list[Recorded]],
) -> None:
    url, seen = grafana
    result = _import(url, "grafanacloud-prom", "grafanacloud-logs", token="glsa_test")
    assert result.returncode == 0, result.stderr
    folder, group = seen
    assert (folder.method, folder.path) == ("POST", "/api/folders")
    assert folder.body == {"uid": "ai-trainer", "title": "ai-trainer"}
    assert (group.method, group.path) == (
        "PUT",
        "/api/v1/provisioning/folder/ai-trainer/rule-groups/ai-trainer",
    )
    for request in seen:
        assert request.headers["Authorization"] == "Bearer glsa_test"
    sent = json.dumps(group.body)
    assert PROMETHEUS_UID not in sent
    assert LOKI_UID not in sent
    assert {q["datasourceUid"] for r in group.body["rules"] for q in r["data"]} == {
        "grafanacloud-prom",
        "grafanacloud-logs",
        "__expr__",
    }
    assert [r["uid"] for r in group.body["rules"]] == list(RULES)
    assert "imported 8 rules" in result.stdout
    assert "glsa_test" not in result.stdout + result.stderr


def test_a_second_import_finds_the_folder_and_replaces_the_group(
    grafana: tuple[str, list[Recorded]],
) -> None:
    url, seen = grafana
    for _ in range(2):
        assert _import(url, "prometheus", "loki").returncode == 0
    assert [r.method for r in seen] == ["POST", "PUT", "POST", "PUT"]
    # No token, as on the local otel-lgtm, which lets anyone in as an admin.
    assert all("Authorization" not in r.headers for r in seen)


@pytest.mark.parametrize("uids", [("prometheus",), ("prom etheus", "loki"), ("prom", "lo/ki")])
def test_an_import_with_missing_or_odd_uids_sends_nothing(
    grafana: tuple[str, list[Recorded]], uids: tuple[str, ...]
) -> None:
    url, seen = grafana
    result = _import(url, *uids)
    assert result.returncode != 0
    assert "usage" in result.stderr
    assert seen == []


def test_a_refused_import_fails() -> None:
    result = _import("http://127.0.0.1:1", "prometheus", "loki")
    assert result.returncode != 0
    assert "import-alerts: result=failed" in result.stderr
