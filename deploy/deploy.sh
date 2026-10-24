#!/usr/bin/env bash
# The deploy poller (ADR-0023 "Pull-based deploy on the server", ticket #225). The server pulls
# approved releases, so GitHub holds no credential to it. Started every two minutes by
# ai-trainer-deploy.timer; deploy/RUNBOOK.md, section 10, installs it.
#
# Each run looks at the newest `production` deployment created by github-actions[bot] (the
# `release` job, once the owner approves it). Only when its latest status is `queued` does it:
#   1. verify the image's provenance against release.yml (a failure stops here: nothing pulled);
#   2. mark it `in_progress` and pull the image by digest;
#   3. take a pre-migration backup (backup.sh predeploy, ADR-0023 invariant 5);
#   4. run `migrate`, then start the app and Caddy on the new image;
#   5. poll /health through Cloudflare and Caddy;
#   6. report `success`, or restore the previous app image and report `failure`.
# The database is never rolled back: migrations are expand-only (ADR-0024 invariant 4), so the
# previous release runs on the new schema. Older deployments are never deployed, so an old
# approval can't take production back to an older release.
#
# Settings:
#   SITE_HOST               the host of PUBLIC_BASE_URL (/opt/ai-trainer/deploy.env)
#   BACKUP_*, RCLONE_*      backup.sh's settings (/opt/ai-trainer/backup.env)
#   CREDENTIALS_DIRECTORY   set by systemd's LoadCredential=; holds `github-token`, a
#                           fine-grained token for this repository only (Deployments read and
#                           write, Attestations read). It's passed to gh alone, per call.
#   DEPLOY_ROOT             optional; default /opt/ai-trainer
set -euo pipefail

REPO=stanislawK/ai-trainer
IMAGE=ghcr.io/stanislawk/ai-trainer
BOT='github-actions[bot]'
# A release tag, as the tag ruleset admits only admins to create (ADR-0023).
TAG='^v[0-9][0-9A-Za-z.+-]*$'
VERIFY_TRIES=3
VERIFY_INTERVAL=10
WAIT_TIMEOUT=120
HEALTH_POLLS=30
HEALTH_INTERVAL=3

die() {
  echo "deploy: $*" >&2
  exit 2
}

root=${DEPLOY_ROOT:-/opt/ai-trainer}
[[ -n ${SITE_HOST:-} ]] || die "SITE_HOST is not set"
token_file=${CREDENTIALS_DIRECTORY:-}/github-token
[[ -n ${CREDENTIALS_DIRECTORY:-} && -r $token_file ]] ||
  die "no github-token credential (LoadCredential= in ai-trainer-deploy.service)"

# One run at a time. systemd never starts the unit while it's still running, and this covers a
# run started by hand as well.
exec 9>"$root/.deploy.lock"
if ! flock -n 9; then
  echo "deploy: another deploy is running; nothing to do"
  exit 0
fi

export GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1
# The token is read for each call and given to that gh process only: it's never exported, so
# docker, curl and backup.sh never see it, and nothing here prints it. GH_DEBUG is blanked, so a
# debug setting left in an env file can't put gh's HTTP log in the journal.
gh_() {
  GH_DEBUG='' GH_TOKEN=$(<"$token_file") gh "$@"
}

compose() {
  docker compose --project-directory "$root" -f "$root/compose.prod.yaml" "$@"
}

# The newest deployment the release workflow created, as "<id> <ref> <digest>", where a ref that
# isn't a plain release tag reads `-`. Anyone else's deployment, a user's or another app's, is
# skipped. A failed read (an expired token, GitHub down) fails the unit, so it shows in
# `systemctl --failed`.
newest=$(gh_ api "repos/$REPO/deployments?environment=production&per_page=100" --jq "
  [.[] | select(.creator.login == \"$BOT\" and .creator.type == \"Bot\")] | first
  | select(. != null)
  | \"\(.id) \((.ref | strings | select(test(\"$TAG\"))) // \"-\") \((.payload | objects | .digest) // \"-\")\"")
[[ -n $newest ]] || exit 0
read -r id ref digest <<<"$newest"

# Acted on only while its latest status is the release job's own `queued`: a status anyone else
# posts, including this script's, never starts a deploy.
state=$(gh_ api "repos/$REPO/deployments/$id/statuses?per_page=1" --jq "
  .[0] | select(.creator.login == \"$BOT\") | .state")
[[ $state == queued ]] || exit 0

report() {
  gh_ api --method POST "repos/$REPO/deployments/$id/statuses" \
    -f state="$1" -f description="${2:0:140}" --jq .id >/dev/null
}

finished=
fail() {
  echo "deploy: deployment $id: $1" >&2
  finished=1
  report failure "$1"
  exit 1
}

# Stopped any other way, by TimeoutStartSec= or an unexpected error, the deployment would stay
# `in_progress`, and only a `queued` one is ever picked up again. Once the new app has been
# started, the previous one is started again first.
switched=
previous=
previous_short=
# shellcheck disable=SC2317  # called from the EXIT trap only
stopped() {
  local why="deploy.sh stopped before it finished"
  if [[ -n $switched && -n $previous ]]; then
    APP_IMAGE=$previous compose up -d --wait --wait-timeout "$WAIT_TIMEOUT" --no-deps app || true
    why="$why; rolled back to ${previous_short}"
  fi
  report failure "$why"
}
trap 'exit 143' TERM
trap '[[ -n $finished ]] || stopped' EXIT

echo "deploy: deployment $id is queued"
# Only the owner can create a `v*` tag (ADR-0023). A branch run of an edited release.yml could
# attest an image and create a deployment as github-actions[bot] too, but not from a tag.
[[ $ref =~ $TAG ]] || fail "the deployment's ref is not a release tag"
[[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || fail "the payload's digest is not a sha256 digest"
image="$IMAGE@$digest"
short=${digest:0:19}
previous=$(cat "$root/app-image" 2>/dev/null || true)
previous_short=${previous#"$IMAGE@"}
previous_short=${previous_short:0:19}

# A failed status is final, so a network blip gets two more tries before it ends the release.
for ((try = 1; ; try++)); do
  if verified=$(gh_ attestation verify "oci://$image" -R "$REPO" \
    --signer-workflow "$REPO/.github/workflows/release.yml" --source-ref "refs/tags/$ref" \
    --deny-self-hosted-runners 2>&1); then
    echo "$verified"
    break
  fi
  echo "$verified" >&2
  ((try < VERIFY_TRIES)) || fail "attestation verification failed for $short; not pulled: $(tail -n 1 <<<"$verified")"
  sleep "$VERIFY_INTERVAL"
done

report in_progress "deploying $short"
echo "deploy: deploying $image"

docker pull "$image" || fail "image pull failed for $short; nothing changed"
APP_IMAGE=$image compose up -d --wait --wait-timeout "$WAIT_TIMEOUT" db ||
  fail "the database did not start; nothing migrated"
"$(dirname "$0")/backup.sh" predeploy || fail "the pre-deploy backup failed; nothing migrated"
APP_IMAGE=$image compose run --rm migrate || fail "the migration failed; the app is unchanged"

healthy() {
  local poll
  for ((poll = 1; poll <= HEALTH_POLLS; poll++)); do
    # /health answers 200 even when Postgres is unreachable, with "database":"unavailable".
    [[ $(curl -fsS --max-time 10 "https://$SITE_HOST/health") == *'"database":"ok"'* ]] && return 0
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

# --no-deps: `migrate` has run already, and Caddy only needs (re)starting on a first deploy.
# --wait-timeout: a crash-looping app goes back to `starting` on each restart, so --wait alone
# would wait for it until the unit's timeout.
switched=1
if APP_IMAGE=$image compose up -d --wait --wait-timeout "$WAIT_TIMEOUT" --no-deps app caddy &&
  healthy; then
  echo "$image" >"$root/app-image"
  finished=1
  report success "deployed $short"
  echo "deploy: deployment $id succeeded: $image"
  exit 0
fi

# The previous image's alembic doesn't know the new head, so --no-deps keeps `migrate` out of
# the rollback. The schema stays as migrated.
[[ -n $previous ]] || fail "health check failed for $short; no previous release to roll back to"
if APP_IMAGE=$previous compose up -d --wait --wait-timeout "$WAIT_TIMEOUT" --no-deps app &&
  healthy; then
  fail "health check failed for $short; rolled back to $previous_short"
fi
fail "health check failed for $short; rolled back to $previous_short, unhealthy too"
