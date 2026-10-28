# ADR 0030 — Production observability on Grafana Cloud

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-25 |
| Related | PRD-0008 (G12, G13, G14), ADR-0018 (supersedes its rejected alternative "A Grafana Alloy collector in compose"), ADR-0023 (supersedes its "Observability … recommended at go-live, but not required" line), ADR-0027, ADR-0029 |

## Context

The first production deploy (#227, M1z, G12) needs the owner to learn of an outage, a failed backup (G13) or a slow log turn (G14) without logging in to the host. Today:

- The app exports only Pydantic AI agent spans, and only when `OTEL_ENABLED` is on (ADR-0018). It exports no HTTP, database or outbound-call spans, and no metrics.
- Logs are uvicorn's default text on stdout, with no trace or request ID.
- Caddy, Postgres and the deploy, backup and purge timers log only to the host (`json-file`, journald).
- Nothing checks the site from outside, and nothing alerts.

ADR-0023 calls Grafana Cloud "recommended, not required". ADR-0018 rejected a collector in compose because a direct OTLP export needed none, but a direct export from the app can't carry Caddy, Postgres, journald or host signals.

The owner's `solid` project already ships to Grafana Cloud through Grafana Alloy from a Hetzner host. This ADR takes that pipeline and adds what it lacks: structured logs, correlation, dashboards and alerts.

## Decision

**Telemetry is required for go-live.** All of it goes to one Grafana Cloud stack through one Grafana Alloy container in `compose.prod.yaml`.

**Pipeline.**
- **App → Alloy:** the app sends OTLP/HTTP to `http://alloy:4318`. `OTEL_EXPORTER_OTLP_ENDPOINT` points at it, and no Grafana credential is in the app's `.env`.
- **Alloy → Grafana Cloud:**
  - **OTLP traces and app metrics** are batched and sent through `otelcol.exporter.otlphttp` to the stack's OTLP gateway. Grafana's docs say to use the HTTP exporter for Grafana Cloud ([Alloy, *OpenTelemetry to LGTM*](https://grafana.com/docs/alloy/latest/collect/opentelemetry-to-lgtm-stack/)).
  - **Container logs** come in through `discovery.docker` + `loki.source.docker`, labelled by compose service, and go out through `loki.write`.
  - **Journal entries** come in through `loki.source.journal`, keeping the `ai-trainer-deploy`, `ai-trainer-backup` and `ai-trainer-purge` syslog identifiers, and go out through `loki.write` too.
  - **Host metrics** come from `prometheus.exporter.unix`, reading the host's `/proc`, `/sys` and `/`, which are mounted read-only and set through `procfs_path`, `sysfs_path` and `rootfs_path`.
  - **Caddy metrics** are scraped from its admin endpoint.
  - **Postgres metrics** come from `prometheus.exporter.postgres` through a read-only `pg_monitor` role.
  - All three sets of Prometheus metrics go out through `prometheus.remote_write`.
- **Config file:** `deploy/alloy/config.alloy`, committed. The image is pinned by digest, like every other service (ADR-0023).

**Networks.**
- Alloy publishes no port.
- Alloy does **not** join `edge`. ADR-0023 trusts `X-Forwarded-*` from that whole subnet because Caddy is its only other member there.
- A new `internal: true` `telemetry` network joins only `app` and `alloy`.
- A second `internal: true` network, `metrics`, joins only `caddy` and `alloy`, for Caddy's metrics (#273).
- Alloy joins `backend` for Postgres, and its own `egress` network for Grafana Cloud.

**Secrets.**
- Values: `GRAFANA_CLOUD_OTLP_URL`/`_USER`, `GRAFANA_CLOUD_LOGS_URL`/`_USER`, `GRAFANA_CLOUD_METRICS_URL`/`_USER`, plus one `GRAFANA_CLOUD_TOKEN`. The token comes from an access policy limited to `traces:write`, `logs:write` and `metrics:write`.
- They live in `${CONFIG_DIR}/alloy.env`, read only by Alloy through `sys.env()`.
- The Postgres monitor password goes in the same file.

**App instrumentation, still only in the composition root (ADR-0018 invariant 3).**
- The tracer provider becomes global.
- The FastAPI instrumentor excludes `/health`.
- SQLAlchemy is instrumented on `engine.sync_engine`, because the engine is async.
- httpx is instrumented for OpenRouter and the external lookups.
- A `MeterProvider` exports over OTLP:
  - HTTP request rate, errors and duration.
  - Model calls by template, model and outcome, with latency and cost, from the same values as the `llm_calls` row.
  - ADR-0029 refusals by limit kind.
- The resource carries `service.name`, `service.version` (the release tag) and `deployment.environment`.
- `Settings` sets a trace sampling ratio (parent-based).

**Logs are JSON lines on stdout.**
- `LOG_FORMAT` (`json` in production, `text` by default) and `LOG_LEVEL` in `Settings`.
- One logging config covers app and uvicorn loggers.
- A stdlib filter adds `trace_id` and `span_id` from the current span, plus a `request_id` that is also returned as `X-Request-ID`.
- When signed in, the opaque `user_id` is added too.
- `/health` access lines are dropped.
- Caddy keeps access logging off. The app's request line is the access log, so client IPs never leave the host.

**Dashboards and alerts are code; Grafana Cloud is configured by the owner.**
- `deploy/grafana/` holds the dashboard JSON, the alert-rule export and the Synthetic Monitoring check definition. The check is an HTTP probe of `/health` through Cloudflare that requires `"database":"ok"` in the body (ADR-0023). It is deferred, and go-live doesn't need it (amended 2026-10-28, see *External check deferred* in Consequences).
- `deploy/RUNBOOK.md` gives the owner's steps to create the stack and token, import the files, set the email contact point and test-fire an alert.
- Alerts cover:
  - the site down for 2 minutes (deferred with the check, amended 2026-10-28)
  - a 5xx rate above 5%
  - log-turn p95 above 10 s (G14)
  - a model-call error rate
  - no backup success line in 26 h (G13)
  - disk above 80%
  - no telemetry received from the host in 15 min, so a dead Alloy is itself an alert (amended 2026-10-26, #277; see Consequences)

### Invariants

1. Only Alloy holds Grafana Cloud credentials, in `${CONFIG_DIR}/alloy.env`. The app's `.env`, the repo, the image and CI never do.
2. Alloy publishes no port and is never on the `edge` network.
3. No log line, span, metric label or Alloy-shipped entry carries an email, a name, an athlete's message or a model's reply, a prompt, a key, a device position (ADR-0027) or a client IP.
   - A log line may carry the opaque internal `user_id`.
   - Spans and metric labels carry no user identity at all (ADR-0018 invariant 4).
4. Metric labels are bounded sets: route templates, never raw paths. Template IDs, model IDs, outcomes and limit kinds are allowed; user IDs and request IDs are not.
5. Telemetry stays off by default and in tests. An unreachable Alloy or Grafana Cloud never fails a request, a deploy, a backup or a purge (ADR-0018 invariant 5).
6. Agents never sign in to Grafana Cloud, call its API or import into it. They change only the files in `deploy/grafana/` and `deploy/alloy/` (ADR-0023 invariant 1).

## Alternatives considered

- **Direct OTLP from the app (ADR-0018 as written):** leaves Caddy, Postgres, the timers and the host invisible. The timers are exactly the backup failures G13 must surface.
- **Separate exporters** (node-exporter, postgres-exporter, promtail): several containers doing what one Alloy does. Promtail is deprecated in favour of Alloy ([Grafana, *Promtail*](https://grafana.com/docs/loki/latest/send-data/promtail/)).
- **Terraform for dashboards and alerts:** a new tool, state file and API token to look after for one instance. JSON imports are reviewable without any of those.
- **Sentry or GlitchTip** (still wired into `solid`): a vendor SDK in application code (ADR-0018 invariant 3). Exceptions already arrive as span events and error logs.

## Consequences

- Once this is Accepted, ADR-0018's Alloy alternative and ADR-0023's "recommended, not required" line are marked superseded by this ADR.
- `CLAUDE.md` gains a stack line. `.claude/rules/python.md` and `.claude/rules/operability.md` carry invariants 1–4.
- Tickets, which all block #227:
  - JSON logs with correlation IDs
  - request-to-database tracing
  - the Alloy relay
  - host, Caddy and Postgres metrics
  - job outcomes in the journal
  - app and model-call metrics
  - the dashboard
  - the alerts
  - refusing `LLM_DEBUG_LOG` outside localhost (ADR-0018's open item, more pressing once logs leave the host)
- **Account deletion (G7).** A deleted athlete's `user_id` can stay in Grafana Cloud logs until they age out under the stack's retention. The ID is opaque and points at nothing once the row is gone. This is the same trade ADR-0018 made for traces.
- **Gotchas carried from `solid`:**
  - A duplicate YAML key there silently turned telemetry off, because the later key won.
  - Its `prometheus.exporter.unix` ran without host mounts, so it most likely reported the Alloy container rather than the VPS.
  - Its Alloy Helm chart was unpinned.
- **Docker socket.** `loki.source.docker` needs `/var/run/docker.sock`. Even mounted read-only, the Docker API is root-equivalent, so Alloy's only listeners are on `telemetry` and the socket must never be exposed further. The alternative is tailing `/var/lib/docker/containers/*/*-json.log` with `local.file_match`, which drops the compose-service labels.
- **Found in the relay ticket (#272):**
  - `loki.source.docker` misses most containers that exit within a few seconds: in a test, 2 of 6 sequential 2.5 s containers were shipped, and Alloy logged `could not inspect container info` for the rest. `migrate` runs that briefly. Its output reaches Grafana Cloud through the deploy unit's journal (`compose run` under `deploy.sh`), not through the Docker source.
  - The Alloy image's `/var/lib/alloy` belongs to its `alloy` user, mode 770. Alloy runs as root with every capability dropped, so it can't enter it and crash-loops on start. Its storage lives at `/data`, a volume Docker creates root-owned. Compose's `--wait` still reports it healthy, because it has no healthcheck.
- **Found in the job-outcomes ticket (#274):**
  - `matches` combines terms with AND ([Alloy, `loki.source.journal`](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.journal/)), so one `loki.relabel` `keep` rule selects the three syslog identifiers and sets the `job` label (`backup`, `purge`, `deploy`).
  - `ai-trainer-backup.service` had no `SyslogIdentifier=`, so the journal named it `backup.sh`; it now sets `ai-trainer-backup`.
  - Each job ends on one outcome line: `<job>: result=ok …`, or `<job>: result=failed …` on stderr with a non-zero exit. `purge` fails before its CLI starts when the app container is stopped, so its unit prints the failed line from `ExecStopPost=`. `backup` does the same for a run systemd kills on a timeout, because `KillSignal=SIGKILL` gives the script no chance to print one. An idle deploy poll prints nothing.
  - Alloy mounts `/var/log/journal`, `/run/log/journal` and `/etc/machine-id` read-only. Whether Debian 13's journal is persistent on the CX23 is still unverified: RUNBOOK 11.3 gives the owner the check and the fix.
- **Found in the metrics ticket (#275):**
  - The `MeterProvider` is not made global. The FastAPI, httpx and SQLAlchemy instrumentors record metrics on the global provider, and theirs can carry a host or, under the old semantic conventions, a raw target. With it left unset they record none, so the app's own instruments are the only series: `http.server.request.duration`, `ai_trainer.llm.call.duration` and `.cost`, `ai_trainer.rate_limit.refusals` and `ai_trainer.log_turn.duration`.
  - The request metric is recorded next to the access line, so a refusal from the gate, the host check or a rate limit is counted too. A method outside HTTP's own set is `_OTHER`, and a path no route matches is `unmatched`.
  - The model-call metric wraps the `llm_calls` repository and is recorded before the row is written, so a failed write still counts the call. A call the budget refuses writes no row and records only a refusal (ADR-0029).
  - The budget's refusals are split into `model_calls_per_hour` and `model_calls_in_flight`, and the request caps into `sign_in` and `session`.
  - The log-turn duration runs from the athlete message's `created_at` to the first fresh reply that carries a draft. A replayed reply records nothing. A log whose draft follows a choice card isn't timed yet.
  - Histogram buckets are in seconds, given as advisories. The log turn's are dense around G14's 5 s and 10 s. Metrics export every 60 s, because Grafana Cloud bills active series per minute.
- **Found in the host, Caddy and Postgres metrics ticket (#273):**
  - **Caddy's metrics have their own network.** They are served by a dedicated site block, `http://10.222.0.10:9180`, bound to Caddy's fixed address on `metrics`. The admin API stays on `localhost:2019`.
    - Caddy is not put on `telemetry`. It would then share two networks with the app, and Docker's DNS could answer `app` with the app's `telemetry` address. Uvicorn doesn't trust `X-Forwarded-*` from that address (ADR-0023).
    - The `:443` site has no `metrics` directive, so `/metrics` there reaches the app, whose sign-in gate answers 401.
    - `internal: true` alone didn't isolate the listener. Under OrbStack, a container on `edge` reached `10.222.0.10:9180` through the host's routing, arriving from the bridge's gateway, `10.222.0.1`. So Alloy has a fixed address too, `10.222.0.20`, and the site serves metrics only to that `remote_ip`; anyone else gets 403.
    - The two servers are named `public` (443) and `metrics`, so the `server` label doesn't depend on the order of the sites. Alloy drops `server="metrics"`, its own scrapes.
  - **The monitor role.** `ai_trainer_monitor` is created by an expand-only migration (`e8b1f4c7a2d9`) with `LOGIN` and `pg_monitor`, and no password, since no secret is in the repo.
    - `deploy/monitor-password.sh` sets the password. It reads only the `POSTGRES_MONITOR_PASSWORD` line of `alloy.env`, never the Grafana keys. The deploy poller runs it after `migrate`, where a failure only warns, and so does `make smoke-up`.
    - The password goes to `psql` on stdin, in a session with statement logging off: Postgres logs a failed statement's text by default, and Alloy ships the db's logs.
    - The password is 24 or more letters and digits, so it needs no escaping in the exporter's DSN.
    - A role belongs to the cluster, so the migration keeps one that already exists, but resets its attributes (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`).
    - Residual: through `pg_read_all_stats`, the role can read every session's `pg_stat_activity.query`. SQLAlchemy binds values as parameters, so the text holds `$1` placeholders rather than athlete data. The collectors Alloy runs don't read it at all.
  - **Host metrics.** Mounting the host's `/proc` exposes the host's PID namespace, so `/host/proc/1/mounts` is the host's mount table without `pid: host`.
    - The network collectors stay off: `/proc/self/net` is still Alloy's own network namespace.
    - Only the root filesystem is read (`mount_points_exclude = "^/.+"`). The mount table also holds every container's rootfs, which would start new series at every deploy.
    - `overlay` is allowed, because a VM's root can be one. The smoke run's is, under OrbStack.
  - **What leaves the host.** One keep list on `__name__` and one fixed `instance` label send about thirty names, each with bounded labels (`mountpoint="/"`, `datname`, Caddy's `server`, `cpu`/`mode`, `device`).
    - Caddy's request histograms are dropped: the app's `http.server.request.duration` already covers latency.
    - The Postgres exporter runs only its database-wide collectors (`database`, `locks`, `stat_database`). The rows of `template0`, `template1` and `postgres` are dropped.
  - **`make alloy-check`** mounts the Docker VM's `/proc`, `/sys` and `/` the way compose does, because `prometheus.exporter.unix` opens them when it is built.
- **Found in the alerts ticket (#277):**
  - **The rules.** `deploy/grafana/alerts.json` is one Grafana-managed rule group, written as the body of the provisioning API's `PUT /api/v1/provisioning/folder/<uid>/rule-groups/<group>`.
    - Grafana Cloud has no file provisioning, so `deploy/grafana/import-alerts.sh` sends that same body both to Grafana Cloud (the owner, with a service-account token) and to the local otel-lgtm (`make alerts-import`). Grafana's docs mark the endpoint deprecated in favour of the App Platform alerting APIs; when it goes, the script moves and the file stays.
    - The datasource UIDs are placeholders, `__PROMETHEUS_UID__` and `__LOKI_UID__`, which the script fills from its arguments. otel-lgtm's UIDs (`prometheus`, `loki`) aren't Grafana Cloud's.
  - **A failed line alerts on its own.** A rule for "no `result=ok` in 26 h" can't fire on a `result=failed` line that follows a good backup within that window. So a second rule, *Scheduled job failed*, fires on any `backup`, `purge` or `deploy` `result=failed` line in the last hour, one alert per `job`.
  - **The probe.** The app keeps `/health` out of its request metric and its access log, so only the Synthetic Monitoring probe sees the site go down.
    - `/health` answers `200` even when the database is down (`{"database":"unavailable"}`), so the check's body regex is what fails.
    - The rule takes `max` over the probe locations, so it fires only when every location fails.
    - otel-lgtm has no Synthetic Monitoring. On the smoke run, `deploy/grafana/smoke-probe.alloy` runs the same request as a blackbox module: the AOP client certificate, through Caddy's 443. It writes `probe_success` under the check's `job` and `instance`, in its own container outside the compose project, so Alloy never ships its lines. A unit test pins it to `synthetic-health.json`.
  - **No traffic is no data, not NaN.** The 5xx, model-error and log-turn rules divide or take a quantile over a window that can be empty. Their denominator carries `> 0`, so an idle window returns no series, and `noDataState: OK` keeps it quiet. Site down uses `noDataState: Alerting` instead, because there, silence is the failure; *No telemetry in 15 min* gets the same effect from `absent_over_time`.
  - **What a rule may name.** `tests/unit/test_grafana_alerts.py` builds what the stack emits from `OtelMetrics` (named as OTLP becomes Prometheus: `http_server_request_duration_seconds_count`, `job` from `service.name`), Alloy's keep list and fixed `instance`, the probe's labels, the `service` and `job` stream labels, and the scripts that print each job's outcome lines. Every metric, label, label value and line filter in a rule must be in that set.
  - **Amended 2026-10-26, at the owner's direction (#277): the dead-Alloy alert is on metrics, not logs.**
    - The Decision first said "no log line received from the host in 15 min". On the idle smoke stack, Loki then received no line at all for 80 minutes while Alloy was healthy. The app drops `/health` lines, Caddy has no access log, and an idle deploy poll prints nothing. So that rule would page on most quiet nights.
    - The rule is now *No telemetry in 15 min*: `absent_over_time(node_load1{instance="ai-trainer-prod", job="integrations/unix"}[15m])`. Alloy writes host metrics every 60 s whatever the traffic, and one token covers logs, metrics and traces, so a dead Alloy, an expired token or a dead host fires it.
    - A break that stops only logs, such as a wrong Loki URL, shows up as *No backup in 26 h*, because the backup's outcome is itself a log line.
    - That takes up to 26 hours, and until then *Scheduled job failed* is blind. A break in the app's OTLP export alone, such as a wrong OTLP URL or a token without the traces or metrics scope, alerts nothing: the app-metric rules treat no data as OK. RUNBOOK 11.4 checks both paths at setup, and the owner checks them again after changing `alloy.env`.
  - Probe IDs belong to the stack, so `synthetic-health.json` leaves `probes` empty and its target as `https://__SITE_HOST__/health`. The owner fills both in, or creates the check in the UI from the same values (RUNBOOK 11.7).
- **Found in the dashboard ticket (#276):**
  - **One file for both stacks.** `deploy/grafana/dashboards/overview.json` names no datasource UID. It uses three datasource variables, each picked by a name regex that matches the stack's default: `Prometheus` or `grafanacloud-<stack>-prom`, and the same for Loki and Tempo. The regexes leave out Grafana Cloud's other datasources of the same types (`grafanacloud-usage`, `-alert-state-history`, `-usage-insights`). The owner imports the file in the UI (RUNBOOK 11.9), and a re-import overwrites by its uid, `ai-trainer-overview`.
  - **The trace link is in the dashboard, not the datasource.** otel-lgtm's Loki has a `trace_id` derived field, but a Grafana Cloud stack's default Loki has none. So the error table extracts the JSON line's fields, and its `trace_id` column carries a data link to Explore on `${tempo}`.
  - **An unhandled exception's line has no `trace_id`.** Uvicorn's `Exception in ASGI application` line is written after the request's span has closed, so the correlation filter finds no span. It still has the `request_id`. The request's access line does have the `trace_id`, so the error panel shows ERROR and CRITICAL lines plus access lines with `status >= 500`, and the table has a `request_id` column to pair them.
  - **Per-day panels are instant queries.** Grafana aligns a range query's points to its step: with a `1d` step the newest falls on a UTC midnight, and with a week's auto step it is up to a step old, so a release's first calls didn't show yet. Calls, errors, cost and refusals are therefore bar gauges, evaluated at the end of the range.
  - **A series' first export is invisible to `increase` and `rate`.** The app exports cumulative counters every 60 s, and its `instance` label is a new `service.instance.id` at every start, so every deploy starts new series. A new series' first sample already holds whatever came before it, and Prometheus takes that sample as the baseline. On the smoke run, `increase` read about half the calls `llm_calls` holds, and 0 of a 10-refusal burst. The per-day panels count each counter as `(increase(x[1d]) and x offset 1d) or last_over_time(x[1d])`: a series new within the day counts by its last value, since it started at 0. That matched `llm_calls` row for row, and all 10 refusals. The `rate`-based panels and the 5xx and model-error alert ratios still miss up to the first minute after each deploy.
  - **Zero, not no data.** A counter nothing has incremented has no series. The 5xx ratio's numerator, the model errors and the refusals each fall back to `vector(0)`, or to the calls' own series times 0, and refusals label that zero `limit_kind="none"`.
  - **The shared catalogue.** `tests/unit/grafana_catalogue.py` holds what the stack emits, as built for #277, plus the app's JSON log fields, taken from `JsonFormatter` itself. Both `test_grafana_alerts.py` and `test_grafana_dashboard.py` check every query against it. Its reader also follows `| json` label filters, with `or` and numeric comparisons, and Grafana's `$__rate_interval`/`$__range`.
- **External check deferred,** amended 2026-10-28 at the owner's direction, during the first release (#227):
  - **Why.** Synthetic Monitoring's public probes reach `/health` through Cloudflare as bots, so Bot Fight Mode challenges them. The owner keeps Bot Fight Mode on, and the Free plan can't exempt a path from it (ADR-0023 *Health check inside the stack*). Allowing the probes' addresses isn't an option either: every Grafana Cloud customer shares them, and they change ([Grafana community](https://community.grafana.com/t/synthetic-monitoring-blocked-by-cloudflare-bot-protection/61982)).
  - **What changes.** Go-live needs no external `/health` check and no *Site down* rule. The owner accepted the gap: nothing alerts on "the host is up but the site is unreachable through Cloudflare". *No telemetry in 15 min* still fires when the host or Alloy is down.
  - **Until a follow-up ticket.** `synthetic-health.json`, `smoke-probe.alloy` and the *Site down* rule stay in `deploy/grafana/`. That rule has `noDataState: Alerting`, so without the check it fires once imported. The owner pauses it after each import (RUNBOOK 11.7). The follow-up removes it or replaces it with a probe that Bot Fight Mode doesn't see.
- **Flagged, to resolve in the tickets:**
  - Grafana Cloud free-tier quotas (active series, log GB, retention, Synthetic Monitoring executions) were not checked here. The owner checks them on the stack page before the dashboard and alert tickets, and the sampling ratio and drop rules are tuned to fit.
