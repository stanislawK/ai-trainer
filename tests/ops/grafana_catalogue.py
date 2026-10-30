"""What the stack emits, and a reader for the queries that ask for it (ADR-0030, #276, #277).

A query for a series nothing sends answers with no data, and nothing says so: an alert never
fires, a panel stays empty. So the alert rules (`deploy/grafana/alerts.json`) and the dashboard
(`deploy/grafana/dashboards/overview.json`) are checked against one catalogue: the app's
instruments, Alloy's keep list, the probe, the shipped log streams, the jobs' outcome lines and
the fields of the app's JSON log lines."""

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram as HistogramData
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from ai_trainer.adapters.log_records import JsonFormatter
from ai_trainer.adapters.metrics import OtelMetrics
from ai_trainer.domain.llm_calls import LlmCallOutcome, NewLlmCall
from ai_trainer.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
GRAFANA = ROOT / "deploy/grafana"
CHECK: dict[str, Any] = json.loads((GRAFANA / "synthetic-health.json").read_text())
ALLOY = (ROOT / "deploy/alloy/config.alloy").read_text()


def alloy_string(name: str, text: str) -> list[str]:
    """Every `<name> = "<value>"` in an Alloy file, decoded like an Alloy (Go) string literal."""
    return [
        json.loads(literal) for literal in re.findall(rf'\b{name}\s*=\s*("(?:[^"\\]|\\.)*")', text)
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
    keep = next(r for r in alloy_string("regex", ALLOY) if r.startswith("node_("))
    instance = frozenset({alloy_string("replacement", ALLOY.split('"kept"', 1)[1])[-1]})
    families: dict[str, Labels] = {
        "node_filesystem_": {
            "job": frozenset({"integrations/unix"}),
            # The exporter reads the root filesystem only (`mount_points_exclude`).
            "mountpoint": frozenset({"/"}),
            "device": None,
            "fstype": None,
        },
        "node_cpu_": {
            "job": frozenset({"integrations/unix"}),
            "cpu": None,
            # `node_cpu_seconds_total`'s modes, from the exporter's /proc/stat columns.
            "mode": frozenset(
                {"user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"}
            ),
        },
        "node_disk_": {"job": frozenset({"integrations/unix"}), "device": None},
        # Load, memory and pressure: one series per host.
        "node_": {"job": frozenset({"integrations/unix"})},
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
_JOBS = next(r for r in alloy_string("regex", ALLOY) if r.startswith("ai-trainer-("))
STREAMS: list[Labels] = [
    {"service": frozenset(COMPOSE["services"])},
    {"job": frozenset(re.fullmatch(r"ai-trainer-\((.+)\)", _JOBS)[1].split("|"))},  # type: ignore[index]
]
EMITTERS = {
    "backup": ["deploy/backup.sh", "deploy/systemd/ai-trainer-backup.service"],
    "purge": ["src/ai_trainer/cli.py", "deploy/systemd/ai-trainer-purge.service"],
    "deploy": ["deploy/deploy.sh"],
}


def outcome_lines(job: str) -> dict[str, list[str]]:
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


def _app_log_fields() -> Labels:
    """The fields of the app's JSON log line (`service="app"`), from its own formatter given a
    record that carries every optional field: what `| json` can turn into labels."""
    try:
        raise ValueError
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, "", 0, "m", None, sys.exc_info())
    for name in ("trace_id", "span_id", "request_id", "user_id", "method", "route", "status"):
        setattr(record, name, "x")
    record.duration_ms = 1.0
    line = json.loads(JsonFormatter().format(record))
    fields: Labels = dict.fromkeys(line)
    fields["level"] = frozenset(logging.getLevelNamesMapping()) - {"NOTSET", "WARN", "FATAL"}
    return fields


APP_LOG_FIELDS = _app_log_fields()


# ---------------------------------------------------------------------------------------------
# A small reading of PromQL and LogQL: enough for the shapes these queries use, and strict about
# anything else, so a query the reader can't follow fails instead of passing unchecked.

_STRING = r'"(?:[^"\\]|\\.)*"'
_SELECTOR = re.compile(r"([A-Za-z_:][\w:]*)?\{([^}]*)\}")
_MATCHER = re.compile(rf"^\s*(\w+)\s*(=~|!~|!=|=)\s*({_STRING})\s*$")
_GROUPING = re.compile(r"\b(?:by|without|on|ignoring)\s*\(([^)]*)\)")
# `| json` and the label filters after it: `| level=~"ERROR|CRITICAL" or status >= 500`.
_FILTER = rf"(\w+)\s*(=~|!~|!=|>=|<=|=|>|<)\s*({_STRING}|\d+)"
_PARSED = re.compile(rf"\|\s*json\b((?:\s*(?:\||\bor\b)\s*{_FILTER})*)")
_FUNCTIONS = {
    "sum", "max", "min", "avg", "count", "rate", "increase", "histogram_quantile",
    "count_over_time", "absent_over_time", "last_over_time", "vector", "label_replace", "by",
    "without", "on", "ignoring", "and", "or", "unless", "bool",
}  # fmt: skip


_AGGREGATION = re.compile(r"\b(?:sum|max|min|avg|count)\s+by\s*\(([^)]*)\)\s*\(")


def _parenthesized(expr: str, start: int) -> str:
    """The text inside the parenthesis that opens at `start`."""
    depth = 0
    for i in range(start, len(expr)):
        depth += {"(": 1, ")": -1}.get(expr[i], 0)
        if depth == 0:
            return expr[start + 1 : i]
    raise AssertionError(f"unbalanced parentheses in {expr!r}")


@dataclass
class Query:
    selectors: list[tuple[str | None, list[tuple[str, str, str]]]] = field(default_factory=list)
    grouping: set[str] = field(default_factory=set)
    # Each `<op> by (labels) (...)`: its labels and the expression it aggregates.
    aggregations: list[tuple[set[str], str]] = field(default_factory=list)
    # The labels `label_replace(..., "<label>", ...)` adds.
    added: set[str] = field(default_factory=set)
    line_filters: list[str] = field(default_factory=list)
    json: bool = False
    parsed: list[tuple[str, str, str | int | float]] = field(default_factory=list)


def parse(expr: str) -> Query:
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
    for match in _AGGREGATION.finditer(expr):
        labels = {label.strip() for label in match[1].split(",") if label.strip()}
        query.aggregations.append((labels, _parenthesized(expr, match.end() - 1)))
    query.added = {json.loads(s) for s in re.findall(rf"label_replace\([^,]+,\s*({_STRING})", expr)}
    query.line_filters = [json.loads(s) for s in re.findall(rf"\|=\s*({_STRING})", expr)]
    for clause in _PARSED.finditer(expr):
        query.json = True
        for label, op, value in re.findall(_FILTER, clause[1]):
            query.parsed.append((label, op, json.loads(value)))
    rest = _PARSED.sub("", expr)
    rest = re.sub(_STRING, "", _GROUPING.sub("", _SELECTOR.sub("", rest)))
    rest = re.sub(r"\[\w+\]", "", rest)
    rest = re.sub(r"\boffset\s+\d+[smhdwy]\b", "", rest)
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


def check_prometheus_query(where: str, expr: str) -> None:
    """Every metric, label and label value the query names is one the stack emits."""
    query = parse(expr)
    assert query.selectors
    assert not query.line_filters
    assert not query.json
    grouped: Labels = {}
    for name, matchers in query.selectors:
        assert name is not None, f"{where}: a selector without a metric name"
        assert name in PROMETHEUS, f"{where}: nothing emits {name}"
        _check_matchers(matchers, PROMETHEUS[name], f"{where} {name}")
        grouped |= PROMETHEUS[name]
    assert query.grouping <= set(grouped), f"{where}: groups by {query.grouping - set(grouped)}"
    # Each `by` keeps only labels the series it aggregates carry, not any series in the query.
    for labels, inner in query.aggregations:
        carried = {
            label for name, _ in parse(inner).selectors if name for label in PROMETHEUS[name]
        }
        assert labels <= carried, f"{where}: groups by {labels - carried} over {inner!r}"


def check_legend(where: str, legend: str, expr: str) -> None:
    """Each `{{label}}` in a legend is one the query's result keeps: a `by` label, a label
    `label_replace` adds, or, with no aggregation, a label of its series."""
    query = parse(expr)
    if re.search(r"\b(?:sum|max|min|avg|count)\b", expr):
        kept = {label for labels, _ in query.aggregations for label in labels} | query.added
    else:
        kept = {label for name, _ in query.selectors if name for label in PROMETHEUS[name]}
    named = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", legend))
    assert named <= kept, f"{where}: legend {legend!r} names {named - kept}; kept: {sorted(kept)}"


def check_loki_query(where: str, expr: str, *, one_outcome: bool) -> Query:
    """The stream selector matches shipped streams, each line filter is text the selected jobs
    print, and each `| json` field is one the app's lines carry.

    With `one_outcome`, a filter must also pick exactly one outcome, as an alert on it does: every
    line of that outcome each job prints, and none of the other."""
    query = parse(expr)
    assert len(query.selectors) == 1, f"{where}: one stream selector"
    name, matchers = query.selectors[0]
    assert name is None
    selected = {label for label, _, _ in matchers}
    streams = [stream for stream in STREAMS if selected <= set(stream)]
    assert streams, f"{where}: no stream carries all of {sorted(selected)}"
    _check_matchers(matchers, streams[0], where)
    assert query.grouping <= selected, f"{where}: groups by a label it doesn't select on"
    if query.json:
        assert matchers == [("service", "=", "app")], f"{where}: | json on lines not the app's"
        # A number compares a numeric field (`status >= 500`); a string names values.
        numeric = [(label, op, v) for label, op, v in query.parsed if isinstance(v, int)]
        for label, _, _ in numeric:
            assert label in APP_LOG_FIELDS, f"{where} | json: no line carries {label!r}"
        strings = [(label, op, v) for label, op, v in query.parsed if isinstance(v, str)]
        _check_matchers(strings, APP_LOG_FIELDS, f"{where} | json")
    jobs = {v for label, op, value in matchers if label == "job" for v in _values(op, value) or ()}
    assert not query.line_filters or jobs, f"{where}: a line filter on lines of no known job"
    for text in query.line_filters:
        picked = set()
        for job in sorted(jobs):
            lines = outcome_lines(job)
            matched = {
                outcome for outcome, printed in lines.items() for line in printed if text in line
            }
            assert matched, f"{where}: no {job} line contains {text!r}"
            if not one_outcome:
                continue
            assert len(matched) == 1, f"{where}: {text!r} matches both ok and failed {job} lines"
            [outcome] = matched
            missed = [line for line in lines[outcome] if text not in line]
            assert not missed, f"{where}: {text!r} misses {missed}"
            picked.add(outcome)
        assert not one_outcome or len(picked) == 1, f"{where}: {text!r} picks {sorted(picked)}"
    return query
