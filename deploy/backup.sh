#!/usr/bin/env bash
# Streams `pg_dump -Fc` through age into R2 (ADR-0023 "Backups", ticket #223). Encrypted to the
# owner's public key, so neither the server nor R2 can read a backup; the private key stays
# offline with the owner.
#
#   backup.sh nightly     the systemd timer: daily/, or weekly/ on a Sunday (UTC)
#   backup.sh predeploy   deploy.sh, before `migrate` (ADR-0023 invariant 5): predeploy/, and the
#                         same encrypted file kept on the server, newest three (#225)
#
# Settings come from the environment (/opt/ai-trainer/backup.env on the server, see
# deploy/backup.env.example):
#   BACKUP_DEST            the rclone destination, e.g. `r2:ai-trainer-backups`
#   BACKUP_AGE_RECIPIENT   the owner's age public key (age1…)
#   BACKUP_DB_CONTAINER    optional; the Postgres container, default ai-trainer-prod-db-1
#   BACKUP_LOCAL_DIR       optional; predeploy's local copies, default
#                          /opt/ai-trainer/backups/predeploy
#   RCLONE_CONFIG_R2_*     the `r2` remote, configured by environment alone
#
# Beside each dump goes `<same name>.counts.age`: every public table's row count, one
# `name count` line each, encrypted the same way. The restore drill (deploy/restore-drill.sh,
# #224) compares a restored backup against it. A psql session exports its REPEATABLE READ
# snapshot, counts in it, and keeps it open while `pg_dump --snapshot` dumps exactly that
# snapshot, so writes during the dump can't make the two disagree.
#
# The dump never touches disk: it goes through pipes and a FIFO only. The R2 bucket locks each
# object for its retention window, so a truncated one couldn't be removed; if the dump or the
# encryption fails, rclone is stopped before it sees the end of its input, and an upload that
# never ends is never stored.
set -euo pipefail
umask 077

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
manifest="${object%.dump.age}.counts.age"
local_copy=
if [[ $kind == predeploy ]]; then
  local_dir=${BACKUP_LOCAL_DIR:-/opt/ai-trainer/backups/predeploy}
  mkdir -p "$local_dir"
  local_copy="$local_dir/ai-trainer-$stamp.dump.age"
fi

# Replaces the value of every credential-looking variable in the environment with ***, so a
# tool's error message that echoes one never reaches the journal.
redact() {
  awk 'BEGIN { for (name in ENVIRON) if (name ~ /(KEY|SECRET|TOKEN|PASS)/ && length(ENVIRON[name]) >= 8) secrets[name] = ENVIRON[name] }
    { for (name in secrets) { s = secrets[name]; while ((i = index($0, s)) > 0) $0 = substr($0, 1, i - 1) "***" substr($0, i + length(s)) } print }'
}

# The row counts, one statement, so they all come from the transaction's one snapshot. The
# restore drill runs the same query on the restored database.
count_sql="SELECT format('%s %s', c.relname, (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]) FROM pg_class c JOIN pg_namespace s ON s.oid = c.relnamespace WHERE s.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname;"

work=$(mktemp -d)
rclone_pid=
psql_pid=
cleanup() {
  [[ -n $rclone_pid ]] && kill -KILL "$rclone_pid" 2>/dev/null
  [[ -n $psql_pid ]] && kill -KILL "$psql_pid" 2>/dev/null
  [[ -n $local_copy ]] && rm -f "$local_copy.partial"
  rm -rf "$work"
}
trap cleanup EXIT

# The snapshot's session, over two FIFOs (no coproc: the drill's tests run this on macOS's
# bash 3.2 too). All four statements go in one write, before psql can have failed; the write
# runs in a subshell that ignores SIGPIPE, so a psql that has already died can't kill this
# script, and its EOF is what's reported.
mkfifo "$work/psql.in" "$work/psql.out"
docker exec -i "$container" psql -X -q -A -t -v ON_ERROR_STOP=1 --username=ai_trainer ai_trainer \
  <"$work/psql.in" >"$work/psql.out" &
psql_pid=$!
exec 4>"$work/psql.in" 5<"$work/psql.out"
(
  trap '' PIPE
  printf '%s\n' 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;' 'SELECT pg_export_snapshot();' \
    "$count_sql" "SELECT '-- end';" >&4
) 2>/dev/null || true
counts=
ended=
if read -r -u 5 snapshot; then
  while read -r -u 5 line; do
    if [[ $line == '-- end' ]]; then ended=1; break; fi
    counts+="$line"$'\n'
  done
fi
if [[ -z $ended ]]; then
  echo "backup: could not count rows; nothing uploaded" >&2
  exit 1
fi

mkfifo "$work/upload"
# rclone's own messages go to a file in the private work dir (mode 0700), and through redact
# once it has finished.
rclone rcat "$object" <"$work/upload" 2>"$work/rclone.err" &
rclone_pid=$!
exec 3>"$work/upload"

upload_failed() {
  rclone_pid=
  redact <"$work/rclone.err" >&2
  echo "backup: upload failed: $1" >&2
  exit 1
}

# predeploy also keeps the encrypted stream on the server; it's renamed into place only once the
# upload has succeeded too.
keep() {
  if [[ -n $local_copy ]]; then tee "$local_copy.partial"; else cat; fi
}

if ! docker exec "$container" pg_dump --format=custom --username=ai_trainer \
  --snapshot="$snapshot" ai_trainer |
  age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" | keep >&3; then
  # fd 3 is still open, so rclone never saw the end of its input and can't have stored the
  # object. Killed here it exits 137. Any other status means it had already failed on its own
  # and broken the pipe under age: that's the upload failing. A dead rclone may not be reaped
  # yet, so whether `kill` succeeds says nothing.
  kill -KILL "$rclone_pid" 2>/dev/null || true
  status=0
  wait "$rclone_pid" 2>/dev/null || status=$?
  [[ $status -eq 137 ]] || upload_failed "$object"
  rclone_pid=
  echo "backup: dump or encryption failed; nothing uploaded" >&2
  exit 1
fi
exec 3>&-

# pg_dump is done with the snapshot: end its transaction.
(
  trap '' PIPE
  printf '%s\n' 'COMMIT;' >&4
) 2>/dev/null || true
exec 4>&- 5<&-
wait "$psql_pid" || true
psql_pid=

wait "$rclone_pid" || upload_failed "$object"
rclone_pid=
echo "backup: uploaded $object"

# After the dump, so a manifest never stands for a backup that isn't there.
printf '%s' "$counts" | age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" |
  rclone rcat "$manifest" 2>"$work/rclone.err" || upload_failed "$manifest"
echo "backup: uploaded $manifest"

if [[ -n $local_copy ]]; then
  mv "$local_copy.partial" "$local_copy"
  copies=("$local_dir"/ai-trainer-*.dump.age)
  if ((${#copies[@]} > 3)); then
    rm -f -- "${copies[@]:0:${#copies[@]}-3}"
  fi
  echo "backup: kept $local_copy"
fi
