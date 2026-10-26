"""`deploy/grafana/` alerts the owner on an outage or a failed job (ADR-0030, ticket #277).

`alerts.json` is one Grafana-managed rule group, the body of the provisioning API's rule-group
PUT, which `deploy/grafana/import-alerts.sh` sends with the stack's own datasource UIDs.
`synthetic-health.json` is the Synthetic Monitoring check behind the site-down rule, and
`smoke-probe.alloy` its stand-in on the smoke run, where no Synthetic Monitoring exists.

A rule that queries a series nothing sends never fires, and nothing says so. So every metric,
label, label value and line filter a rule names is checked against what the stack emits: the
app's instruments, Alloy's keep list, the probe and the jobs' outcome lines."""

import json
import re
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram as HistogramData
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from ai_trainer.adapters.metrics import OtelMetrics
from ai_trainer.domain.llm_calls import LlmCallOutcome, NewLlmCall
from ai_trainer.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
GRAFANA = ROOT / "deploy/grafana"
GROUP: dict[str, Any] = json.loads((GRAFANA / "alerts.json").read_text())
RULES: dict[str, dict[str, Any]] = {rule["uid"]: rule for rule in GROUP["rules"]}
CHECK: dict[str, Any] = json.loads((GRAFANA / "synthetic-health.json").read_text())
PROBE = (GRAFANA / "smoke-probe.alloy").read_text()
ALLOY = (ROOT / "deploy/alloy/config.alloy").read_text()
IMPORT = GRAFANA / "import-alerts.sh"

PROMETHEUS_UID = "__PROMETHEUS_UID__"
LOKI_UID = "__LOKI_UID__"


def _alloy_string(name: str, text: str) -> list[str]:
    """Every `<name> = "<value>"` in an Alloy file, decoded like an Alloy (Go) string literal."""
    return [
        json.loads(literal) for literal in re.findall(rf'\b{name}\s*=\s*("(?:[^"\\]|\\.)*")', text)
    ]


def _queries(rule: dict[str, Any]) -> list[tuple[str, str]]:
    """(datasource UID, expression) of each query a rule runs, its expressions left out."""
    return [
        (query["datasourceUid"], query["model"]["expr"])
        for query in rule["data"]
        if query["datasourceUid"] != "__expr__"
    ]


# ---------------------------------------------------------------------------------------------
# What the stack emits. A label maps to its possible values, or None where any value can occur
# (a route template, a model ID).

type Labels = dict[str, frozenset[str] | None]


def _prometheus_names(name: str, unit: str, histogram: bool) -> dict[str, bool]:
    """The series an OTLP instrument becomes in Prometheus, and whether each carries `le`.

    Both Grafana Cloud's OTLP gateway and otel-lgtm translate with underscores and suffixes:
    dots become underscores, the unit is appended (`s` as `seconds`, an annotation such as
    `{refusal}` not at all), and a counter ends in `_total`."""
    base = name.replace(".", "_")
    if unit and not unit.startswith("{"):
        base += "_" + {"s": "seconds"}.get(unit, unit)
    if histogram:
        return {f"{base}_bucket": True, f"{base}_count": False, f"{base}_sum": False}
    return {f"{base}_total": False}


def _app_series() -> dict[str, Labels]:
    """Each instrument of `OtelMetrics`, recorded once, with the labels it records under."""
    reader = InMemoryMetricReader()
    metrics = OtelMetrics(MeterProvider(metric_readers=[reader]).get_meter("ai_trainer"))
    metrics.request(method="GET", route="/chat", status=200, seconds=0.1)
    metrics.model_call(
        NewLlmCall(
            user_id=uuid4(),
            template_id="router",
            template_version=1,
            model="z-ai/glm-5.3-flash",
            input_tokens=1,
            output_tokens=1,
            cost=Decimal("0.0001"),
            latency_ms=100,
            outcome=LlmCallOutcome.SUCCESS,
        )
    )
    metrics.refusal("session")
    metrics.log_turn(3)
    data = reader.get_metrics_data()
    assert data is not None
    # The resource's `service.name` is the series' `job` (OTEL_SERVICE_NAME in production).
    resource: Labels = {"job": frozenset({Settings.model_fields["otel_service_name"].default})}
    resource["instance"] = None
    values: dict[str, frozenset[str]] = {
        "http_response_status_class": frozenset(f"{n}xx" for n in range(1, 6)),
        # A refused call writes no row, so it is never recorded as a model call (ADR-0029).
        "outcome": frozenset(o.value for o in LlmCallOutcome) - {LlmCallOutcome.RATE_LIMITED},
    }
    series: dict[str, Labels] = {}
    for metric in (m for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics):
        keys = {k.replace(".", "_") for p in metric.data.data_points for k in p.attributes or {}}
        labels: Labels = {**resource, **{key: values.get(key) for key in keys}}
        histogram = isinstance(metric.data, HistogramData)
        for name, bucket in _prometheus_names(metric.name, metric.unit or "", histogram).items():
            series[name] = {**labels, "le": None} if bucket else labels
    assert len(series) == 11, sorted(series)
    return series


def _alloy_series() -> dict[str, Labels]:
    """Host, Caddy and Postgres series: the names Alloy's keep list lets out, under the fixed
    `instance` it sets and each exporter's own labels."""
    keep = next(r for r in _alloy_string("regex", ALLOY) if r.startswith("node_("))
    instance = frozenset({_alloy_string("replacement", ALLOY.split('"kept"', 1)[1])[-1]})
    families: dict[str, Labels] = {
        "node_filesystem_": {
            "job": frozenset({"integrations/unix"}),
            # The exporter reads the root filesystem only (`mount_points_exclude`).
            "mountpoint": frozenset({"/"}),
            "device": None,
            "fstype": None,
        },
        "node_": {
            "job": frozenset({"integrations/unix"}),
            "cpu": None,
            "mode": None,
            "device": None,
        },
        "caddy_": {"job": frozenset({"integrations/caddy"}), "server": frozenset({"public"})},
        "pg_": {"job": frozenset({"integrations/postgres"}), "datname": frozenset({"ai_trainer"})},
    }

    class Kept(dict[str, Labels]):
        def __missing__(self, name: str) -> Labels:
            if not re.fullmatch(keep, name):
                raise KeyError(name)
            family = next(labels for prefix, labels in families.items() if name.startswith(prefix))
            return {**family, "instance": instance}

        def __contains__(self, name: object) -> bool:
            return isinstance(name, str) and re.fullmatch(keep, name) is not None

    return Kept()


def _probe_series() -> dict[str, Labels]:
    """Synthetic Monitoring's `probe_success`: `job` and `instance` are the check's job and
    target, `probe` the location that ran it."""
    return {
        "probe_success": {
            "job": frozenset({CHECK["job"]}),
            "instance": frozenset({CHECK["target"]}),
            "probe": None,
            "config_version": None,
            "check_name": frozenset({"http"}),
        }
    }


class Emitted(dict[str, Labels]):
    def __init__(self, *parts: dict[str, Labels]) -> None:
        super().__init__()
        self.parts = parts

    def __contains__(self, name: object) -> bool:
        return any(name in part for part in self.parts)

    def __getitem__(self, name: str) -> Labels:
        for part in self.parts:
            if name in part:
                return part[name]
        raise KeyError(name)


PROMETHEUS = Emitted(_app_series(), _alloy_series(), _probe_series())

# Loki has two kinds of stream, and no stream carries both labels: container lines by compose
# service (`service`), and the jobs' journal lines by `job` (ADR-0030, #274).
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "compose.prod.yaml").read_text())
_JOBS = next(r for r in _alloy_string("regex", ALLOY) if r.startswith("ai-trainer-("))
STREAMS: list[Labels] = [
    {"service": frozenset(COMPOSE["services"])},
    {"job": frozenset(re.fullmatch(r"ai-trainer-\((.+)\)", _JOBS)[1].split("|"))},  # type: ignore[index]
]
EMITTERS = {
    "backup": ["deploy/backup.sh", "deploy/systemd/ai-trainer-backup.service"],
    "purge": ["src/ai_trainer/cli.py", "deploy/systemd/ai-trainer-purge.service"],
    "deploy": ["deploy/deploy.sh"],
}


def _outcome_lines(job: str) -> dict[str, list[str]]:
    """The outcome lines a job prints, by outcome: the text of each `echo` or `write`, with its
    variables left in. Comments describe the lines too, so they don't count. deploy.sh prints
    `result=$1` for both outcomes."""
    printed = "\n".join((ROOT / path).read_text() for path in EMITTERS[job])
    lines: dict[str, list[str]] = {"ok": [], "failed": []}
    for line in re.findall(rf'(?:echo |write\(f)"({job}: result=[^"]*)"', printed):
        for outcome in (
            ("ok", "failed") if "result=$1" in line else (line.split("result=")[1].split()[0],)
        ):
            lines[outcome].append(line.replace("result=$1", f"result={outcome}"))
    assert lines["ok"], job
    assert lines["failed"], job
    return lines


# ---------------------------------------------------------------------------------------------
# A small reading of PromQL and LogQL: enough for the shapes these rules use, and strict about
# anything else, so a query the reader can't follow fails instead of passing unchecked.

_STRING = r'"(?:[^"\\]|\\.)*"'
_SELECTOR = re.compile(r"([A-Za-z_:][\w:]*)?\{([^}]*)\}")
_MATCHER = re.compile(rf"^\s*(\w+)\s*(=~|!~|!=|=)\s*({_STRING})\s*$")
_GROUPING = re.compile(r"\b(?:by|without|on|ignoring)\s*\(([^)]*)\)")
_FUNCTIONS = {
    "sum", "max", "min", "avg", "count", "rate", "increase", "histogram_quantile",
    "count_over_time", "absent_over_time", "by", "without", "on", "ignoring", "and", "or",
    "unless", "bool",
}  # fmt: skip


@dataclass
class Query:
    selectors: list[tuple[str | None, list[tuple[str, str, str]]]] = field(default_factory=list)
    grouping: set[str] = field(default_factory=set)
    line_filters: list[str] = field(default_factory=list)


def _parse(expr: str) -> Query:
    query = Query()
    for name, body in _SELECTOR.findall(expr):
        matchers = []
        for part in filter(str.strip, re.split(rf",(?=(?:[^\"]|{_STRING})*$)", body)):
            match = _MATCHER.match(part)
            assert match, f"unreadable matcher {part!r} in {expr!r}"
            matchers.append((match[1], match[2], json.loads(match[3])))
        query.selectors.append((name or None, matchers))
    for labels in _GROUPING.findall(expr):
        query.grouping |= {label.strip() for label in labels.split(",") if label.strip()}
    query.line_filters = [json.loads(s) for s in re.findall(rf"\|=\s*({_STRING})", expr)]
    rest = re.sub(_STRING, "", _GROUPING.sub("", _SELECTOR.sub("", expr)))
    rest = re.sub(r"\[\w+\]", "", rest)
    words = set(re.findall(r"[A-Za-z_][\w:]*", rest))
    assert words <= _FUNCTIONS, f"{words - _FUNCTIONS} in {expr!r}: a bare metric or a new function"
    assert not re.search(r"\|[~!]|!=|\|\s*(json|logfmt|line_format)", rest), "only |= filters"
    return query


def _values(op: str, value: str) -> set[str] | None:
    """The values a matcher names: one for `=`/`!=`, each alternative of a plain `a|b` regex,
    or None for a regex that names no value (`.+`)."""
    if op in ("=", "!="):
        return {value}
    if re.fullmatch(r"[\w./-]+(\|[\w./-]+)*", value):
        return set(value.split("|"))
    assert value == ".+", f"regex {value!r} is neither a list of values nor .+"
    return None


def _check_matchers(matchers: list[tuple[str, str, str]], labels: Labels, where: str) -> None:
    for label, op, value in matchers:
        assert label in labels, f"{where}: no series carries {label!r}"
        allowed, named = labels[label], _values(op, value)
        if named is None:
            assert op == "=~", f"{where}: {label} {op} {value!r} excludes every value"
        elif allowed is not None:
            # An exclusion that names no real value silently excludes nothing, so it counts too.
            assert named <= allowed, f"{where}: {label} {op} {value!r}; emitted: {sorted(allowed)}"
            if op in ("!=", "!~"):
                assert allowed - named, f"{where}: {label} {op} {value!r} excludes every value"


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
    query = _parse(expr)
    assert query.selectors
    assert not query.line_filters
    grouped: Labels = {}
    for name, matchers in query.selectors:
        assert name is not None, f"{uid}: a selector without a metric name"
        assert name in PROMETHEUS, f"{uid}: nothing emits {name}"
        _check_matchers(matchers, PROMETHEUS[name], f"{uid} {name}")
        grouped |= PROMETHEUS[name]
    assert query.grouping <= set(grouped), f"{uid}: groups by {query.grouping - set(grouped)}"


@pytest.mark.parametrize(("uid", "expr"), LOKI_QUERIES)
def test_each_loki_query_selects_shipped_streams_and_filters_on_printed_lines(
    uid: str, expr: str
) -> None:
    query = _parse(expr)
    assert len(query.selectors) == 1, f"{uid}: one stream selector"
    name, matchers = query.selectors[0]
    assert name is None
    selected = {label for label, _, _ in matchers}
    streams = [stream for stream in STREAMS if selected <= set(stream)]
    assert streams, f"{uid}: no stream carries all of {sorted(selected)}"
    _check_matchers(matchers, streams[0], uid)
    assert query.grouping <= selected, f"{uid}: groups by a label it doesn't select on"
    jobs = {v for label, op, value in matchers if label == "job" for v in _values(op, value) or ()}
    assert not query.line_filters or jobs, f"{uid}: a line filter on lines of no known job"
    # A filter picks one outcome: every line of it that each job prints, and none of the other.
    for text in query.line_filters:
        picked = set()
        for job in sorted(jobs):
            lines = _outcome_lines(job)
            matched = {
                outcome for outcome, printed in lines.items() for line in printed if text in line
            }
            assert matched, f"{uid}: no {job} line contains {text!r}"
            assert len(matched) == 1, f"{uid}: {text!r} matches both ok and failed {job} lines"
            [outcome] = matched
            missed = [line for line in lines[outcome] if text not in line]
            assert not missed, f"{uid}: {text!r} misses {missed}"
            picked.add(outcome)
        assert len(picked) == 1, f"{uid}: {text!r} picks {sorted(picked)} across jobs"


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
    modules = yaml.safe_load(_alloy_string("config", PROBE)[0])["modules"]
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
    assert _alloy_string("address", PROBE) == ["https://host.docker.internal/health"]
    replacements = _alloy_string("replacement", PROBE)
    assert CHECK["job"] in replacements
    assert "https://localhost/health" in replacements
    assert _alloy_string("url", PROBE) == ["http://host.docker.internal:9090/api/v1/write"]


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
