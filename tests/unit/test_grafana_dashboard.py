"""`deploy/grafana/dashboards/overview.json` answers "is it healthy?" after a release (ADR-0030,
ticket #276).

The same file is imported into Grafana Cloud and into the smoke run's otel-lgtm, whose
datasources have other names and UIDs. So the dashboard picks its datasources through three
variables that match each stack's default names, and names no UID of its own.

A panel that queries a series nothing sends stays empty, and nothing says so. So every metric,
label, label value, line filter and log field a panel names is checked against what the stack
emits (`grafana_catalogue`)."""

import json
import re
from typing import Any

import pytest

from tests.unit.grafana_catalogue import (
    APP_LOG_FIELDS,
    GRAFANA,
    check_legend,
    check_loki_query,
    check_prometheus_query,
)

DASHBOARD: dict[str, Any] = json.loads((GRAFANA / "dashboards/overview.json").read_text())
PANELS: dict[str, dict[str, Any]] = {
    panel["title"]: panel for panel in DASHBOARD["panels"] if panel["type"] != "row"
}
VARIABLES = {"prometheus": "prometheus", "loki": "loki", "tempo": "tempo"}

# Grafana fills these in before the query reaches the datasource.
_MACROS = re.compile(r"\$__(rate_interval|interval|range)\b")


def _expand(expr: str) -> str:
    return _MACROS.sub("5m", expr)


TARGETS = [
    (title, target["datasource"]["type"], target["expr"])
    for title, panel in PANELS.items()
    for target in panel["targets"]
]


@pytest.mark.parametrize(("title", "kind", "expr"), TARGETS)
def test_each_panel_query_names_only_what_the_stack_emits(title: str, kind: str, expr: str) -> None:
    if kind == "prometheus":
        check_prometheus_query(title, _expand(expr))
    else:
        assert kind == "loki", title
        check_loki_query(title, _expand(expr), one_outcome=False)


_REQUESTS = 'http_server_request_duration_seconds_count{job="ai-trainer"}'
_BUCKETS = 'http_server_request_duration_seconds_bucket{job="ai-trainer"}'
_COST = 'ai_trainer_llm_call_cost_USD_total{job="ai-trainer"}'
REJECTED = {
    'sum(rate(http_server_requests_total{job="ai-trainer"}[$__rate_interval]))': "nothing emits",
    f"sum by (route) (rate({_REQUESTS}[$__rate_interval]))": "groups by",
    # A grouping label is checked against the selectors it aggregates, not the whole query.
    f"sum by (template_id) (rate({_REQUESTS}[5m])) / sum(rate({_COST}[5m]))": "groups by",
    f"sum by (le) (rate({_REQUESTS}[5m])) or sum by (le) (rate({_BUCKETS}[5m]))": "groups by",
    # Labels are per metric, not per exporter: memory has no cpu, CPU has no device.
    'node_memory_MemTotal_bytes{cpu="0"}': "no series carries 'cpu'",
    'node_cpu_seconds_total{device="sda"}': "no series carries 'device'",
    'sum(rate(node_cpu_seconds_total{mode="idel"}[$__rate_interval]))': "emitted",
    '{service="app"} | json | trace="x"': "no series carries 'trace'",
    '{service="app"} | json | level="FATAL"': "emitted",
    '{service="app"} | json | level="ERROR" or stauts >= 500': "no line carries 'stauts'",
    '{service="caddy"} | json | level="ERROR"': "not the app's",
    '{job="backup"} |= "backup: done"': "no backup line contains",
    '{service="app"} | logfmt': "logfmt",
}


@pytest.mark.parametrize(("expr", "reason"), REJECTED.items())
def test_the_catalogue_rejects_a_panel_query_nothing_answers(expr: str, reason: str) -> None:
    """A metric, label value, log field or level no line carries; `| json` on a service that
    doesn't write JSON; a line no job prints; and a parser the reader doesn't follow."""
    kind = "loki" if expr.startswith("{") else "prometheus"
    with pytest.raises(AssertionError, match=reason):
        test_each_panel_query_names_only_what_the_stack_emits("x", kind, expr)


LEGENDS = [
    (title, target["legendFormat"], target["expr"])
    for title, panel in PANELS.items()
    for target in panel["targets"]
    if "legendFormat" in target
]


@pytest.mark.parametrize(("title", "legend", "expr"), LEGENDS)
def test_each_legend_names_only_labels_its_query_keeps(title: str, legend: str, expr: str) -> None:
    """A `{{label}}` the result doesn't carry renders as an empty name."""
    check_legend(title, legend, _expand(expr))


@pytest.mark.parametrize(
    ("legend", "expr"),
    [
        ("{{template}}", f"sum by (template_id) (increase({_REQUESTS}[1d]))"),
        ("{{route}}", f"sum(rate({_REQUESTS}[5m]))"),
    ],
)
def test_the_catalogue_rejects_a_legend_the_result_has_no_label_for(legend: str, expr: str) -> None:
    with pytest.raises(AssertionError, match="legend"):
        check_legend("x", legend, expr)


def test_the_app_log_fields_come_from_its_formatter() -> None:
    assert {"level", "logger", "message", "trace_id", "request_id", "route", "status"} <= set(
        APP_LOG_FIELDS
    )
    assert APP_LOG_FIELDS["level"] == frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


# ---------------------------------------------------------------------------------------------
# The ticket's panels.

PANEL_QUERIES = {
    "5xx rate": ['http_response_status_class="5xx"', "http_server_request_duration_seconds_count"],
    "Request duration p50 / p95": [
        "histogram_quantile(0.5,",
        "histogram_quantile(0.95,",
        "http_server_request_duration_seconds_bucket",
    ],
    "Log turn p50 / p95 (G14)": [
        "histogram_quantile(0.5,",
        "histogram_quantile(0.95,",
        "ai_trainer_log_turn_duration_seconds_bucket",
    ],
    "Model calls per day": ["sum by (template_id)", "ai_trainer_llm_call_duration_seconds_count"],
    "Model errors per day": ['outcome=~"error|timeout"', "sum by (template_id)"],
    "Model cost per day (USD)": ["sum by (template_id)", "ai_trainer_llm_call_cost_USD_total"],
    "Rate-limit refusals": [
        "sum by (limit_kind)",
        "ai_trainer_rate_limit_refusals_total",
        'label_replace(vector(0), "limit_kind", "none", "", "")',
    ],
    "CPU": ['node_cpu_seconds_total{instance="ai-trainer-prod", mode="idle"}'],
    "Memory": ["node_memory_MemAvailable_bytes", "node_memory_MemTotal_bytes"],
    "Disk": ['node_filesystem_avail_bytes{instance="ai-trainer-prod", mountpoint="/"}'],
    "Postgres connections": ["pg_stat_database_numbackends", "pg_settings_max_connections"],
    "Scheduled job outcomes": ['{job=~"backup|purge|deploy"} |= "result="'],
    # Uvicorn logs an unhandled exception after the request's span has ended, so that line has
    # no trace_id; the request's own access line, status 500, has it (ADR-0030, #276).
    "App errors": ['{service="app"} | json | level=~"ERROR|CRITICAL" or status >= 500'],
}


def _exprs(title: str) -> str:
    return "\n".join(target["expr"] for target in PANELS[title]["targets"])


def test_the_dashboard_holds_exactly_the_ticket_panels() -> None:
    assert sorted(PANELS) == sorted(PANEL_QUERIES)
    for title, fragments in PANEL_QUERIES.items():
        for fragment in fragments:
            assert fragment in _exprs(title), f"{title}: {fragment}"


@pytest.mark.parametrize("title", ["Model calls per day", "Model errors per day",
                                   "Model cost per day (USD)", "Rate-limit refusals"])  # fmt: skip
def test_the_per_day_panels_count_the_24_h_up_to_the_end_of_the_range(title: str) -> None:
    """One instant query, a bar per template or limit kind. A range query's points are aligned to
    its step, so over a week its newest point is up to a step old: a release's first calls would
    not show yet."""
    panel = PANELS[title]
    assert panel["type"] == "bargauge"
    assert "timeFrom" not in panel
    for target in panel["targets"]:
        assert (target["instant"], target["range"]) == (True, False)
        assert "$__" not in target["expr"]
        assert_counts_first_samples(target["expr"])


_DAILY = re.compile(
    r"\(increase\((?P<a>[\w:]+\{[^}]*\})\[1d\]\) and (?P<b>[\w:]+\{[^}]*\}) offset 1d\) "
    r"or last_over_time\((?P<c>[\w:]+\{[^}]*\})\[1d\]\)"
)


def assert_counts_first_samples(expr: str) -> None:
    """Every counter is read as `(increase(x[1d]) and x offset 1d) or last_over_time(x[1d])`.

    The app's `instance` is a new `service.instance.id` at every start, so each deploy starts
    new series, and `increase` can't see what a new series counted before its first export: on
    the smoke run, 10 refusals in one minute read 0. A series new within the day is counted by
    its last value instead, since it started at 0."""
    counters = re.findall(r"[\w:]+(?:_total|_count)\{[^}]*\}", expr)
    found = _DAILY.findall(expr)
    assert found, expr
    for a, b, c in found:
        assert a == b == c, expr
    assert len(counters) == 3 * len(found), expr


@pytest.mark.parametrize(
    "expr",
    [
        'sum(increase(ai_trainer_rate_limit_refusals_total{job="ai-trainer"}[1d]))',
        'sum((increase(a_total{x="1"}[1d]) and a_total{x="2"} offset 1d) '
        'or last_over_time(a_total{x="1"}[1d]))',
    ],
)
def test_a_plain_increase_or_a_mismatched_series_is_not_a_daily_count(expr: str) -> None:
    with pytest.raises(AssertionError):
        assert_counts_first_samples(expr)


@pytest.mark.parametrize("title", ["Model errors per day", "Rate-limit refusals", "5xx rate"])
def test_none_is_a_zero_rather_than_no_data(title: str) -> None:
    """A counter nothing has incremented has no series, so the panel would say No data."""
    assert re.search(r"\bor\b", _exprs(title)), title


def test_the_log_turn_panel_marks_the_g14_bounds() -> None:
    steps = PANELS["Log turn p50 / p95 (G14)"]["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert [step["value"] for step in steps] == [None, 5, 10]
    assert PANELS["Log turn p50 / p95 (G14)"]["fieldConfig"]["defaults"]["unit"] == "s"


def test_the_job_outcomes_show_the_latest_first_over_a_week() -> None:
    panel = PANELS["Scheduled job outcomes"]
    assert panel["type"] == "logs"
    assert panel["timeFrom"] == "7d"
    assert panel["options"]["sortOrder"] == "Descending"


# ---------------------------------------------------------------------------------------------
# Datasources: one file for both stacks.


def _variable(name: str) -> dict[str, Any]:
    variables: list[dict[str, Any]] = DASHBOARD["templating"]["list"]
    [variable] = [v for v in variables if v["name"] == name]
    return variable


@pytest.mark.parametrize(
    ("name", "local", "cloud", "others"),
    [
        ("prometheus", "Prometheus", "grafanacloud-mystack-prom",
         ["grafanacloud-usage", "grafanacloud-ml-metrics", "grafanacloud-mystack-prom-old"]),
        ("loki", "Loki", "grafanacloud-mystack-logs",
         ["grafanacloud-mystack-alert-state-history", "grafanacloud-mystack-usage-insights"]),
        ("tempo", "Tempo", "grafanacloud-mystack-traces", ["grafanacloud-mystack-profiles"]),
    ],
)  # fmt: skip
def test_each_datasource_variable_picks_the_stacks_default_by_name(
    name: str, local: str, cloud: str, others: list[str]
) -> None:
    variable = _variable(name)
    assert (variable["type"], variable["query"], variable["hide"]) == ("datasource", name, 2)
    pattern = re.fullmatch(r"/(.+)/", variable["regex"])
    assert pattern, variable["regex"]
    assert re.search(pattern[1], local)
    assert re.search(pattern[1], cloud)
    for other in others:
        assert not re.search(pattern[1], other), other


def _datasources(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        found = [node["datasource"]] if isinstance(node.get("datasource"), dict) else []
        return found + [d for value in node.values() for d in _datasources(value)]
    if isinstance(node, list):
        return [d for item in node for d in _datasources(item)]
    return []


def test_every_datasource_is_one_of_the_variables_and_no_uid_is_hard_coded() -> None:
    used = {(d["type"], d["uid"]) for d in _datasources(DASHBOARD["panels"])}
    assert used == {("prometheus", "${prometheus}"), ("loki", "${loki}")}
    assert "__inputs" not in DASHBOARD
    assert "grafanacloud" not in json.dumps(DASHBOARD["panels"])
    assert (DASHBOARD["uid"], DASHBOARD["id"]) == ("ai-trainer-overview", None)


# ---------------------------------------------------------------------------------------------
# The trace link.


def test_a_trace_id_in_the_error_panel_opens_its_trace_in_tempo() -> None:
    panel = PANELS["App errors"]
    assert panel["type"] == "table"
    # A burst of the exporter's own errors must not push a request's 5xx line out of the table.
    assert panel["targets"][0]["maxLines"] == 1000
    [override] = [
        o for o in panel["fieldConfig"]["overrides"] if o["matcher"]["options"] == "trace_id"
    ]
    assert override["matcher"]["id"] == "byName"
    [prop] = [p for p in override["properties"] if p["id"] == "links"]
    [link] = prop["value"]
    assert link["url"].startswith("/explore?")
    panes = json.loads(link["url"].split("panes=", 1)[1].split("&", 1)[0])
    [pane] = panes.values()
    [query] = pane["queries"]
    assert pane["datasource"] == query["datasource"]["uid"] == "${tempo}"
    assert (query["queryType"], query["query"]) == ("traceql", "${__value.raw}")
    # The table shows the line's own fields, so the trace_id column exists, and request_id
    # pairs an error line without a trace_id with its request's access line.
    assert panel["transformations"][0]["id"] == "extractFields"
    [organize] = [t for t in panel["transformations"] if t["id"] == "organize"]
    shown = [k for k, v in organize["options"]["includeByName"].items() if v]
    assert {"trace_id", "request_id", "level", "message", "status"} <= set(shown)
