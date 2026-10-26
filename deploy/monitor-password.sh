#!/usr/bin/env bash
# Sets the password of `ai_trainer_monitor`, the login role Alloy's Postgres exporter connects as
# (ADR-0030, ticket #273). The migration creates the role without one, since no secret is in the
# repo; the password lives in alloy.env as POSTGRES_MONITOR_PASSWORD, next to Alloy's other keys.
#
#   deploy/monitor-password.sh <alloy.env> <compose command…>
#
# e.g. `deploy/monitor-password.sh /opt/ai-trainer/alloy.env docker compose -f compose.prod.yaml`.
# deploy.sh runs it after `migrate`, and `make smoke-up` after the stack is up. Run it by hand
# after changing the password, then recreate Alloy (deploy/RUNBOOK.md, section 11.2).
#
# Only that one key is read, and the file is never sourced. The password goes to psql in the
# `db` container on stdin: never an argument, which `ps` would show, and never the output, which
# the journal ships. The session turns off statement logging first, because Postgres logs a
# failed statement's text by default and Alloy ships the db's logs too.
set -euo pipefail

ROLE=ai_trainer_monitor

die() {
  echo "monitor-password: $*" >&2
  exit 2
}

[[ $# -ge 2 ]] || die "usage: deploy/monitor-password.sh <alloy.env> <compose command…>"
env_file=$1
shift
[[ -r $env_file ]] || die "cannot read $env_file"

# The last assignment wins, as compose reads an env file.
password=$(grep -E '^POSTGRES_MONITOR_PASSWORD=' "$env_file" | tail -n 1 | cut -d= -f2- || true)
# Letters and digits only: safe in the SQL below and in the exporter's DSN without escaping.
[[ $password =~ ^[A-Za-z0-9]{24,}$ ]] ||
  die "POSTGRES_MONITOR_PASSWORD in $env_file is missing, or not 24+ letters and digits"

if printf '%s\n' \
  "SET log_min_error_statement = panic;" \
  "SET log_statement = none;" \
  "SET log_min_duration_statement = -1;" \
  "ALTER ROLE $ROLE PASSWORD '$password';" |
  "$@" exec -T db psql -q -v ON_ERROR_STOP=1 -U ai_trainer -d ai_trainer >/dev/null; then
  echo "monitor-password: result=ok role=$ROLE"
else
  echo "monitor-password: result=failed role=$ROLE" >&2
  exit 1
fi
