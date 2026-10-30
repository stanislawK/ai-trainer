#!/usr/bin/env bash
# Restores a backup into a throwaway Postgres and compares every table's row count with the
# counts deploy/backup.sh took in the dump's own snapshot (ADR-0023 "Backups", ticket #224).
# The owner runs it on their own machine, at go-live and then monthly: deploy/RUNBOOK.md,
# section 8.6.
#
#   restore-drill.sh            the latest backup: the newest stamp in daily/, weekly/ or predeploy/
#   restore-drill.sh <path>     that one, e.g. weekly/ai-trainer-20261025T023000Z.dump.age
#
# Settings come from the environment (deploy/restore.env.example):
#   BACKUP_DEST            the rclone destination, e.g. `r2:ai-trainer-backups`
#   RESTORE_AGE_IDENTITY   the owner's offline age key file
#   RCLONE_CONFIG_R2_*     the `r2` remote, with a read-only token
#
# The decrypted dump goes through pipes into the container only. The container has no network
# and keeps its data in memory (tmpfs), and it is removed on every exit: success, failure or an
# interrupt. Exits 0 only when the restore succeeds and every count matches.
#
# Runs on macOS's bash 3.2 too: no coproc, mapfile or associative arrays.
set -euo pipefail
umask 077

# compose.prod.yaml's `db` image, by digest; tests/ops/test_restore_drill_script.py keeps
# them the same.
image=pgvector/pgvector:pg18-trixie@sha256:9d9c930220cb9bf2f956d10a8f909cf9973d9672ca0278aef2c1e6facccad2e0
# backup.sh's count query, verbatim.
count_sql="SELECT format('%s %s', c.relname, (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]) FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace WHERE s.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname;"

die() {
  echo "restore-drill: $*" >&2
  exit 2
}
fail() {
  echo "restore-drill: $*" >&2
  exit 1
}

[[ $# -le 1 ]] || die "usage: restore-drill.sh [<path>.dump.age]"
[[ -n ${BACKUP_DEST:-} ]] || die "BACKUP_DEST is not set"
[[ -n ${RESTORE_AGE_IDENTITY:-} && -r ${RESTORE_AGE_IDENTITY:-} ]] ||
  die "RESTORE_AGE_IDENTITY must name your age key file"
key=$RESTORE_AGE_IDENTITY

if [[ $# -eq 1 ]]; then
  object=$1
else
  listing=$(rclone lsf -R --files-only --include '*.dump.age' "$BACKUP_DEST") ||
    fail "could not list $BACKUP_DEST"
  # The newest by file name, whatever the prefix: the names carry a sortable UTC stamp.
  object=$(printf '%s\n' "$listing" | awk -F/ 'NF { print $NF "\t" $0 }' | sort | tail -n 1 | cut -f 2)
  [[ -n $object ]] || fail "no backup found in $BACKUP_DEST"
fi
[[ $object == *.dump.age ]] || die "usage: restore-drill.sh [<path>.dump.age]"
echo "restore-drill: $object"

# The counts first: a backup without them can't be checked, so nothing is started for it.
manifest="${object%.dump.age}.counts.age"
expected=$(rclone cat "$BACKUP_DEST/$manifest" | age --decrypt --identity "$key") ||
  fail "could not fetch or decrypt $manifest"
[[ -n $expected ]] || fail "$manifest lists no tables"

name="ai-trainer-restore-drill-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
started=
cleanup() {
  if [[ -n $started ]]; then docker rm -f -v "$name" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

started=1
docker run -d --name "$name" --network none --tmpfs /var/lib/postgresql \
  -e POSTGRES_USER=ai_trainer -e POSTGRES_DB=ai_trainer \
  -e POSTGRES_PASSWORD="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" \
  "$image" >/dev/null || fail "could not start the throwaway container"

# Over TCP: on first start the image runs a temporary server for its init scripts that listens
# on the socket only, so a socket check could pass before the real server is up.
tries=0
until docker exec "$name" pg_isready -q -h 127.0.0.1 -U ai_trainer -d ai_trainer >/dev/null 2>&1; do
  tries=$((tries + 1))
  [[ $tries -lt 60 ]] || fail "Postgres did not start in 60 s"
  sleep 1
done

# In the background, so an INT or TERM runs the trap at once instead of after the restore;
# removing the container then ends the exec.
rclone cat "$BACKUP_DEST/$object" | age --decrypt --identity "$key" |
  docker exec -i "$name" pg_restore --single-transaction --no-owner \
    --username=ai_trainer --dbname=ai_trainer &
wait $! || fail "restore failed: $object"

actual=$(docker exec "$name" psql -X -q -A -t -v ON_ERROR_STOP=1 \
  --username=ai_trainer --dbname=ai_trainer -c "$count_sql") ||
  fail "could not count the restored rows"

# One row per table on either side, in the backup's order, then any the restore added. Every
# non-empty line must be one `table count` pair for a table not seen before on its side:
# anything else fails, since skipping it on both sides could let broken counts pass.
printf '%s\n' "$actual" | WANT=$expected awk '
  function readable(line) { return line ~ /^[^ ]+ [0-9]+$/ }
  BEGIN {
    n = split(ENVIRON["WANT"], lines, "\n")
    for (i = 1; i <= n; i++) {
      if (lines[i] == "") continue
      split(lines[i], f, " ")
      if (!readable(lines[i]) || (f[1] in want)) { bad++; continue }
      want[f[1]] = f[2]; order[++tables] = f[1]
    }
  }
  $0 == "" { next }
  !readable($0) || ($1 in have) { bad++; next }
  { have[$1] = $2; if (!($1 in want)) order[++tables] = $1 }
  END {
    if (bad) {
      printf "restore-drill: %d count lines are not one `table count` pair each\n", bad > "/dev/stderr"
      exit 1
    }
    if (!tables) {
      print "restore-drill: no tables to compare" > "/dev/stderr"
      exit 1
    }
    printf "%-32s %10s %10s\n", "table", "backup", "restored"
    for (i = 1; i <= tables; i++) {
      t = order[i]
      w = (t in want) ? want[t] : "-"
      h = (t in have) ? have[t] : "-"
      verdict = (w == h) ? "ok" : "MISMATCH"
      if (verdict != "ok") differ++
      printf "%-32s %10s %10s  %s\n", t, w, h, verdict
    }
    if (differ) {
      printf "restore-drill: %d of %d tables differ\n", differ, tables > "/dev/stderr"
      exit 1
    }
    printf "restore-drill: every count matches (%d tables)\n", tables
  }' || exit 1
