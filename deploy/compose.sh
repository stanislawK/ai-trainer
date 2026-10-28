#!/usr/bin/env bash
# Runs Compose on the production stack, for commands typed on the host (ticket #303):
#
#   /opt/ai-trainer/compose.sh logs app --since 1h
#   /opt/ai-trainer/compose.sh ps
#
# Compose interpolates the whole of compose.prod.yaml before any command, so even `logs caddy`
# fails without APP_IMAGE and SITE_HOST. This reads SITE_HOST from deploy.env, APP_IMAGE from
# app-image and RELEASE_TAG from app-version, the files the poller records after a deploy. A
# value already in the environment wins, so a hand rollback still names its own digest. A host
# never deployed has no app-image and stops here: the `:?` guard in compose.prod.yaml stays, so
# nothing starts an image nobody chose (ADR-0023). deploy.sh and install.sh do not use this; they
# set each image themselves.
#
# Settings:
#   DEPLOY_ROOT   optional; default /opt/ai-trainer
set -euo pipefail

die() {
  echo "compose: $*" >&2
  exit 2
}

root=${DEPLOY_ROOT:-/opt/ai-trainer}

if [[ -r $root/deploy.env ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$root/deploy.env"
  set +a
fi
[[ -n ${SITE_HOST:-} ]] || die "SITE_HOST is not set ($root/deploy.env)"

if [[ -z ${APP_IMAGE:-} ]]; then
  APP_IMAGE=$(cat "$root/app-image" 2>/dev/null || true)
  [[ -n $APP_IMAGE ]] ||
    die "no app-image in $root: nothing is deployed yet; set APP_IMAGE to ghcr.io/stanislawk/ai-trainer@sha256:<digest>"
fi
export APP_IMAGE
if [[ -z ${RELEASE_TAG:-} && -r $root/app-version ]]; then
  RELEASE_TAG=$(<"$root/app-version")
  export RELEASE_TAG
fi

exec docker compose --project-directory "$root" -f "$root/compose.prod.yaml" "$@"
