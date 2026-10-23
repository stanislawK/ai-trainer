#!/usr/bin/env bash
# Streams `pg_dump -Fc` through age into R2 (ADR-0023 "Backups", ticket #223). Encrypted to the
# owner's public key, so neither the server nor R2 can read a backup; the private key stays
# offline with the owner.
#
#   backup.sh nightly     the systemd timer: daily/, or weekly/ on a Sunday (UTC)
#   backup.sh predeploy   deploy.sh, before `migrate` (ADR-0023 invariant 5): predeploy/
#
# Settings come from the environment (/opt/ai-trainer/backup.env on the server, see
# deploy/backup.env.example):
#   BACKUP_DEST            the rclone destination, e.g. `r2:ai-trainer-backups`
#   BACKUP_AGE_RECIPIENT   the owner's age public key (age1…)
#   BACKUP_DB_CONTAINER    optional; the Postgres container, default ai-trainer-prod-db-1
#   RCLONE_CONFIG_R2_*     the `r2` remote, configured by environment alone
#
# The dump never touches disk: it goes through pipes and a FIFO only. The R2 bucket locks each
# object for its retention window, so a truncated one couldn't be removed; if the dump or the
# encryption fails, rclone is stopped before it sees the end of its input, and an upload that
# never ends is never stored.
set -euo pipefail

die() {
  echo "backup: $*" >&2
  exit 2
}

[[ $# -eq 1 && ($1 == nightly || $1 == predeploy) ]] || die "usage: backup.sh nightly|predeploy"
kind=$1
[[ -n ${BACKUP_DEST:-} ]] || die "BACKUP_DEST is not set"
[[ ${BACKUP_AGE_RECIPIENT:-} =~ ^age1[02-9ac-hj-np-z]+$ ]] ||
  die "BACKUP_AGE_RECIPIENT must be an age public key (age1…)"
container=${BACKUP_DB_CONTAINER:-ai-trainer-prod-db-1}

read -r weekday stamp < <(date -u '+%u %Y%m%dT%H%M%SZ')
case $kind in
  predeploy) prefix=predeploy ;;
  *) if [[ $weekday == 7 ]]; then prefix=weekly; else prefix=daily; fi ;;
esac
object="$BACKUP_DEST/$prefix/ai-trainer-$stamp.dump.age"

# Replaces the value of every credential-looking variable in the environment with ***, so a
# tool's error message that echoes one never reaches the journal.
redact() {
  awk 'BEGIN { for (name in ENVIRON) if (name ~ /(KEY|SECRET|TOKEN|PASS)/ && length(ENVIRON[name]) >= 8) secrets[name] = ENVIRON[name] }
    { for (name in secrets) { s = secrets[name]; while ((i = index($0, s)) > 0) $0 = substr($0, 1, i - 1) "***" substr($0, i + length(s)) } print }'
}

work=$(mktemp -d)
rclone_pid=
cleanup() {
  [[ -n $rclone_pid ]] && kill -KILL "$rclone_pid" 2>/dev/null
  rm -rf "$work"
}
trap cleanup EXIT

mkfifo "$work/upload"
# rclone's own messages go to a file in the private work dir (mode 0700), and through redact
# once it has finished.
rclone rcat "$object" <"$work/upload" 2>"$work/rclone.err" &
rclone_pid=$!
exec 3>"$work/upload"

upload_failed() {
  rclone_pid=
  redact <"$work/rclone.err" >&2
  echo "backup: upload failed: $object" >&2
  exit 1
}

if ! docker exec "$container" pg_dump --format=custom --username=ai_trainer ai_trainer |
  age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" >&3; then
  # fd 3 is still open, so rclone never saw the end of its input and can't have stored the
  # object. Killed here it exits 137. Any other status means it had already failed on its own
  # and broken the pipe under age: that's the upload failing. A dead rclone may not be reaped
  # yet, so whether `kill` succeeds says nothing.
  kill -KILL "$rclone_pid" 2>/dev/null || true
  status=0
  wait "$rclone_pid" 2>/dev/null || status=$?
  [[ $status -eq 137 ]] || upload_failed
  rclone_pid=
  echo "backup: dump or encryption failed; nothing uploaded" >&2
  exit 1
fi
exec 3>&-

wait "$rclone_pid" || upload_failed
rclone_pid=
echo "backup: uploaded $object"
