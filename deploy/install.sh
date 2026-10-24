#!/usr/bin/env bash
# Copies the host files the deploy poller does not update (ADR-0023).
# Run on the laptop, from a checkout of this repo:
#
#   deploy/install.sh <ip>
#
# The poller changes only the app image. One list below is the whole copy: each entry is
# source|destination|mode|owner. The remote side installs from that list. Secrets stay out:
# env files, the GitHub token, and the certificate directory.
# The attested image is not a source for these files. They are inside the public image, and
# applying them from it could publish a port or replace this deploy path.
#
# Files land in a private staging directory under /tmp, removed on success and on failure.
# A copy that stops halfway can leave a mix of old and new files; run it again.
# If Caddy is already running, the script recreates it. The proxy config is mounted as one
# file, so a new inode is invisible to the running container, and Caddy reads it at start.
# The first install, before the stack exists, only copies the file.
set -euo pipefail

die() {
  echo "install: $*" >&2
  exit 2
}

[[ $# -eq 1 ]] || die "usage: deploy/install.sh <ip>"
host=$1
# A hostname or IPv4 address. A leading dash would be an ssh flag; an @ would add a second user.
[[ $host =~ ^[A-Za-z0-9.][A-Za-z0-9._-]*$ ]] || die "usage: deploy/install.sh <ip>"

repo=$(cd "$(dirname "$0")/.." && pwd)

# source|destination|mode|owner
files=(
  "compose.prod.yaml|/opt/ai-trainer/compose.prod.yaml|0644|deploy"
  "deploy/Caddyfile|/opt/ai-trainer/deploy/Caddyfile|0644|deploy"
  "deploy/deploy.sh|/opt/ai-trainer/deploy/deploy.sh|0755|deploy"
  "deploy/backup.sh|/opt/ai-trainer/deploy/backup.sh|0755|deploy"
  "deploy/systemd/ai-trainer-deploy.service|/etc/systemd/system/ai-trainer-deploy.service|0644|root"
  "deploy/systemd/ai-trainer-deploy.timer|/etc/systemd/system/ai-trainer-deploy.timer|0644|root"
  "deploy/systemd/ai-trainer-backup.service|/etc/systemd/system/ai-trainer-backup.service|0644|root"
  "deploy/systemd/ai-trainer-backup.timer|/etc/systemd/system/ai-trainer-backup.timer|0644|root"
  "deploy/systemd/ai-trainer-purge.service|/etc/systemd/system/ai-trainer-purge.service|0644|root"
  "deploy/systemd/ai-trainer-purge.timer|/etc/systemd/system/ai-trainer-purge.timer|0644|root"
)

stage=
cleanup_stage() {
  [[ -n $stage ]] || return 0
  ssh "deploy@${host}" "rm -rf $(printf '%q' "$stage")" || true
}
trap cleanup_stage EXIT

stage=$(ssh "deploy@${host}" 'mktemp -d -m 0700 /tmp/ai-trainer-install.XXXXXXXX' | tail -n 1)
if [[ $stage != /tmp/ai-trainer-install.* ]]; then
  stage=
  die "the remote staging directory was not created"
fi

sources=()
manifest=""
for spec in "${files[@]}"; do
  src=${spec%%|*}
  [[ -f $repo/$src ]] || die "missing $src"
  sources+=("$repo/$src")
  manifest="${manifest}$(basename "$src")|${spec#*|}"$'\n'
done

scp "${sources[@]}" "deploy@${host}:${stage}/"

quoted_stage=$(printf '%q' "$stage")
# Wait for the lock. The poller takes it without waiting and skips the tick if it cannot.
ssh "deploy@${host}" "STAGE=${quoted_stage} flock /opt/ai-trainer/.deploy.lock bash -s" <<EOF
set -euo pipefail
trap 'rm -rf "\$STAGE"' EXIT
install -d -m 0750 /opt/ai-trainer/deploy
while IFS='|' read -r base dest mode owner; do
  [[ -n \$base ]] || continue
  if [[ \$owner == root ]]; then
    sudo install -o root -g root -m "\$mode" "\$STAGE/\$base" "\$dest"
  else
    install -m "\$mode" "\$STAGE/\$base" "\$dest"
  fi
done <<'LIST'
${manifest}
LIST
sudo systemctl daemon-reload
# The bind is one file. Replacing it changes the host inode, which the running container
# does not see, and Caddy has already read the old file. Recreate only when it is up.
if [[ -f /opt/ai-trainer/deploy.env ]] &&
  docker ps --format '{{.Names}}' | grep -qx ai-trainer-prod-caddy-1; then
  set -a
  # shellcheck disable=SC1091
  . /opt/ai-trainer/deploy.env
  set +a
  [[ -n \${SITE_HOST:-} ]] || {
    echo "install: SITE_HOST is unset; Caddy was not recreated" >&2
    exit 1
  }
  # Compose interpolates the whole file. migrate and app require APP_IMAGE even for `up caddy`.
  # set +a is already in effect, so the assignment has to be exported.
  if ! app_image=\$(cat /opt/ai-trainer/app-image); then
    echo "install: could not read app-image; Caddy was not recreated" >&2
    exit 1
  fi
  export APP_IMAGE=\$app_image
  docker compose --project-directory /opt/ai-trainer -f /opt/ai-trainer/compose.prod.yaml \
    up -d --no-deps --force-recreate caddy
fi
EOF
