#!/usr/bin/env bash
# The real round trip of deploy/backup.sh and deploy/restore-drill.sh against the local stack
# (tickets #223, #224): a throwaway age key, a directory standing in for the R2 bucket, then
# the drill restores the backup into a throwaway Postgres and compares every table's row count
# with the backup's own. `make backup-check` runs it in Debian 13 with the host's own age and
# rclone. Nothing leaves the machine, and the key is deleted afterwards.
set -euo pipefail

cd "$(dirname "$0")/.."
container=${BACKUP_DB_CONTAINER:-ai-trainer-db-1}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

age-keygen -o "$work/key.txt" 2>/dev/null
BACKUP_DEST="$work/bucket" BACKUP_AGE_RECIPIENT=$(age-keygen -y "$work/key.txt") \
  BACKUP_DB_CONTAINER=$container deploy/backup.sh nightly | tee "$work/backup.out"
# The journal line Alloy ships (ADR-0030, #274) is the run's last.
[[ $(tail -n 1 "$work/backup.out") == 'backup: result=ok kind=nightly' ]] || {
  echo "backup-check: backup.sh did not end with 'backup: result=ok'" >&2
  exit 1
}

dump=$(find "$work/bucket" -type f -name '*.dump.age')
counts=$(find "$work/bucket" -type f -name '*.counts.age')
[[ $(find "$work/bucket" -type f | wc -l) -eq 2 && -n $dump && $counts == "${dump%.dump.age}.counts.age" ]] || {
  echo "backup-check: expected one .dump.age and its .counts.age, found: $(find "$work/bucket" -type f)" >&2
  exit 1
}
BACKUP_DEST="$work/bucket" RESTORE_AGE_IDENTITY="$work/key.txt" deploy/restore-drill.sh
