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
- `deploy/grafana/` holds the dashboard JSON, the alert-rule export and the Synthetic Monitoring check definition. The check is an HTTP probe of `/health` through Cloudflare that requires `"database":"ok"` in the body (ADR-0023).
- `deploy/RUNBOOK.md` gives the owner's steps to create the stack and token, import the files, set the email contact point and test-fire an alert.
- Alerts cover:
  - the site down for 2 minutes
  - a 5xx rate above 5%
  - log-turn p95 above 10 s (G14)
  - a model-call error rate
  - no backup success line in 26 h (G13)
  - disk above 80%
  - no log line received from the host in 15 min, so a dead Alloy is itself an alert

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
- **Flagged, to resolve in the tickets:**
  - `loki.source.journal` needs the host journal mounted (`/var/log/journal`, `/run/log/journal`, `/etc/machine-id`). Whether Debian 13's journal is persistent on the CX23 is unverified.
  - `matches` combines terms with AND ([Alloy, `loki.source.journal`](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.journal/)), so selecting the three identifiers needs a relabel `keep` rule instead.
  - Caddy's admin endpoint listens on `localhost:2019` by default. Exposing metrics only on `telemetry` needs either an admin or metrics listener bound there, or a dedicated site block.
  - Grafana Cloud free-tier quotas (active series, log GB, retention, Synthetic Monitoring executions) were not checked here. The owner checks them on the stack page before the dashboard and alert tickets, and the sampling ratio and drop rules are tuned to fit.
