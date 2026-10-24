#!/usr/bin/env bash
# The real round trip of deploy/backup.sh against the local stack (ticket #223): a throwaway age
# key, a directory standing in for the R2 bucket, then the key decrypts the object and
# `pg_restore --list` reads it. `make backup-check` runs it in Debian 13 with the host's own age
# and rclone. Nothing leaves the machine, and the key is deleted afterwards.
set -euo pipefail

cd "$(dirname "$0")/.."
container=${BACKUP_DB_CONTAINER:-ai-trainer-db-1}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

age-keygen -o "$work/key.txt" 2>/dev/null
BACKUP_DEST="$work/bucket" BACKUP_AGE_RECIPIENT=$(age-keygen -y "$work/key.txt") \
  BACKUP_DB_CONTAINER=$container deploy/backup.sh nightly

objects=$(find "$work/bucket" -type f)
[[ $(wc -l <<<"$objects") -eq 1 && $objects == *.dump.age ]] || {
  echo "backup-check: expected one .dump.age object, found: ${objects:-none}" >&2
  exit 1
}
age --decrypt --identity "$work/key.txt" "$objects" |
  docker exec -i "$container" pg_restore --list >"$work/toc"
tables=$(grep -c ' TABLE public ' "$work/toc" || true)
[[ $tables -gt 0 ]] || { echo "backup-check: pg_restore listed no tables" >&2; exit 1; }
echo "backup-check: ${objects#"$work/bucket/"} decrypts with the test key;" \
  "pg_restore --list reads $(grep -vc '^;' "$work/toc") entries, $tables of them tables"
