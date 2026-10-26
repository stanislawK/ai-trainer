#!/usr/bin/env bash
# Imports alerts.json into a Grafana as the `ai-trainer` rule group of the `ai-trainer` folder
# (ADR-0030, ticket #277). The owner runs it against Grafana Cloud (RUNBOOK 11.7); `make
# alerts-import` runs it against the smoke run's local otel-lgtm. Agents never run it against
# Grafana Cloud (ADR-0030 invariant 6).
#
#   GRAFANA_TOKEN=<service account token> deploy/grafana/import-alerts.sh <grafana url> <prometheus uid> <loki uid>
#
# The two UIDs are the stack's own Prometheus and Loki datasources (Connections → Data sources →
# the UID in each one's URL); they replace alerts.json's placeholders. The token is optional for
# otel-lgtm, which lets anyone in as an admin. It is read from the environment and never
# printed. The folder is created once; every run replaces the whole group, so a rule removed
# from alerts.json is removed from Grafana too.
set -euo pipefail

usage() {
  echo "usage: $0 <grafana url> <prometheus uid> <loki uid>; a UID is letters, digits, - and _" >&2
  exit 2
}

(($# == 3)) || usage
url=${1%/}
prometheus=$2
loki=$3
for uid in "$prometheus" "$loki"; do
  [[ $uid =~ ^[A-Za-z0-9_-]+$ ]] || usage
done

here=$(cd "$(dirname "$0")" && pwd)
body=$(mktemp)
response=$(mktemp)
trap 'rm -f "$body" "$response"' EXIT
sed -e "s/__PROMETHEUS_UID__/$prometheus/g" -e "s/__LOKI_UID__/$loki/g" "$here/alerts.json" >"$body"

# The token goes to curl on stdin, as a config line, so it never appears in a process list.
api() {
  local method=$1 path=$2
  shift 2
  {
    [[ -n ${GRAFANA_TOKEN:-} ]] && printf 'header = "Authorization: Bearer %s"\n' "$GRAFANA_TOKEN"
    true
  } | curl -sS -o "$response" -w '%{http_code}' --config - -X "$method" \
    -H 'Content-Type: application/json' "$@" "$url$path"
}

# Grafana's answer says why it refused, e.g. a folder titled ai-trainer under another UID.
fail() {
  [[ -s $response ]] && head -c 500 "$response" >&2 && echo >&2
  echo "import-alerts: result=failed $1" >&2
  exit 1
}

# 409 or 412 (Grafana 13 answers 412): the folder is already there.
status=$(api POST /api/folders --data '{"uid":"ai-trainer","title":"ai-trainer"}') || fail "reason=unreachable"
[[ $status =~ ^(200|409|412)$ ]] || fail "reason=folder status=$status"

status=$(api PUT /api/v1/provisioning/folder/ai-trainer/rule-groups/ai-trainer --data-binary "@$body") ||
  fail "reason=unreachable"
[[ $status == 200 ]] || fail "reason=group status=$status"

echo "import-alerts: result=ok imported $(grep -c '"uid": "ai-trainer-' "$body") rules"
