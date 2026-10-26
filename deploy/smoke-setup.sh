#!/usr/bin/env bash
# Prepares .smoke/ for the README's local smoke run of compose.prod.yaml (ticket #221): test
# stand-ins for the Cloudflare Origin CA certificate and the zone's AOP CA, a random database
# password, and an app .env derived from your own .env. Test certificates only — production's
# come from Cloudflare and the owner's CA (ADR-0023). Re-running replaces everything in .smoke/.
set -euo pipefail

cd "$(dirname "$0")/.."
SMOKE=.smoke
HOST=localhost

[[ -f .env ]] || { echo "smoke-setup: no .env; copy .env.example to .env and fill it in first" >&2; exit 1; }

rm -rf "$SMOKE"
mkdir -p "$SMOKE/certs"
cd "$SMOKE"

# ca <name>: a self-signed CA, <name>.pem and <name>-key.pem.
ca() {
  openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=ai-trainer smoke $1" \
    -keyout "$1-key.pem" -out "$1.pem" 2>/dev/null
}

# leaf <name> <ca> <extension>: a certificate for <name> signed by <ca>.
leaf() {
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$1" -keyout "$1-key.pem" -out "$1.csr" 2>/dev/null
  openssl x509 -req -in "$1.csr" -CA "$2.pem" -CAkey "$2-key.pem" -CAcreateserial -days 7 \
    -extfile <(printf '%s\n' "$3") -out "$1.pem" 2>/dev/null
  rm "$1.csr"
}

# The server side: what Caddy serves, standing in for the Cloudflare Origin CA certificate.
ca origin-ca
leaf origin origin-ca "subjectAltName=DNS:$HOST"
# The client side: the AOP CA Caddy trusts, a client certificate it issued (Cloudflare's stand-in),
# and one from an unrelated CA that Caddy must refuse.
ca aop-ca
leaf client aop-ca "extendedKeyUsage=clientAuth"
ca rogue-ca
leaf rogue-client rogue-ca "extendedKeyUsage=clientAuth"

mv origin.pem origin-key.pem certs/
cp aop-ca.pem certs/
# Caddy runs as root with every capability dropped, so it can't override file permissions.
chmod 644 certs/*

password=$(openssl rand -hex 24)
printf 'POSTGRES_PASSWORD=%s\n' "$password" > db.env
# The app exports to Alloy (compose.prod.yaml), so any OTLP headers in your .env go nowhere.
sed -e "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://$HOST|" \
    -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+psycopg://ai_trainer:$password@db:5432/ai_trainer|" \
    -e "s|^OTEL_EXPORTER_OTLP_HEADERS=.*|OTEL_EXPORTER_OTLP_HEADERS=|" \
    ../.env > .env

# Alloy relays to a local grafana/otel-lgtm on the host instead of Grafana Cloud (README
# "Production smoke run"); it ignores the credentials. No real Grafana Cloud value goes here.
cat > alloy.env <<'ALLOY'
GRAFANA_CLOUD_OTLP_URL=http://host.docker.internal:4318
GRAFANA_CLOUD_OTLP_USER=local
GRAFANA_CLOUD_LOGS_URL=http://host.docker.internal:3100/loki/api/v1/push
GRAFANA_CLOUD_LOGS_USER=local
GRAFANA_CLOUD_METRICS_URL=http://host.docker.internal:9090/api/v1/write
GRAFANA_CLOUD_METRICS_USER=local
GRAFANA_CLOUD_TOKEN=local
ALLOY
# The monitor role's password; `make smoke-up` sets it in the database (#273).
printf 'POSTGRES_MONITOR_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> alloy.env

echo "smoke-setup: wrote $SMOKE/ (certs/, .env, db.env, alloy.env, client.pem, rogue-client.pem, origin-ca.pem)"
