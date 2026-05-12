#!/usr/bin/env bash
# Inspect the migration script currently running inside the migration-runner
# container, plus current row counts on the ClickHouse Cloud target.
#
# Useful when Step 4 looks frozen in the chat: `run_python` is synchronous
# and only returns stdout when the script exits, so the LibreChat UI shows
# nothing in between. This script peeks at /proc inside the runner to confirm
# the script is still alive and prints live row counts on the target.
#
# Usage:
#   make migration-status          (or)        bash scripts/migration-status.sh
#
# Safe to re-run as often as you like.

set -uo pipefail

if ! docker compose ps --status running migration-runner 2>/dev/null | grep -q migration-runner; then
  echo "❌ migration-runner is not running. Start the playground first: make up"
  exit 1
fi

echo "════════════════════════════════════════════════════════════════"
echo " Migration runner — in-flight Python script"
echo "════════════════════════════════════════════════════════════════"

docker compose exec -T migration-runner sh -c '
  pid=""
  for p in $(ls /proc 2>/dev/null); do
    case "$p" in
      ""|[!0-9]*|1) continue ;;
    esac
    cmd=$(tr "\0" " " < /proc/$p/cmdline 2>/dev/null) || continue
    case "$cmd" in
      *python*/tmp/*.py*) pid="$p"; break ;;
    esac
  done

  if [ -z "$pid" ]; then
    echo "ℹ️  No migration script is currently running."
    exit 0
  fi

  state=$(awk -F: "/^State/ {sub(/^ +/, \"\", \$2); print \$2}" /proc/$pid/status)
  mem=$(awk -F: "/^VmRSS/ {sub(/^ +/, \"\", \$2); print \$2}" /proc/$pid/status)
  rchar=$(awk -F: "/^rchar/ {gsub(/ /, \"\", \$2); print \$2}" /proc/$pid/io)
  wchar=$(awk -F: "/^wchar/ {gsub(/ /, \"\", \$2); print \$2}" /proc/$pid/io)

  echo "✓ Python script running (PID $pid)"
  echo "  State : $state"
  echo "  RSS   : $mem"
  echo "  Read  : $((rchar / 1024 / 1024)) MB (source → script)"
  echo "  Wrote : $((wchar / 1024 / 1024)) MB (script → ClickHouse Cloud)"
'

echo
echo "── ClickHouse Cloud — current row counts ───────────────────────"

if ! docker compose ps --status running clickhousectl-mcp 2>/dev/null | grep -q clickhousectl-mcp; then
  echo "(clickhousectl-mcp not running — skipping target query)"
else
  docker compose exec -T clickhousectl-mcp sh -c '
    curl -s --max-time 10 --user "${CLICKHOUSE_USER}:${CLICKHOUSE_PASSWORD}" \
      "https://${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT}/?query=SELECT+database%2C+name%2C+total_rows%2C+formatReadableSize(total_bytes)+AS+size+FROM+system.tables+WHERE+engine+NOT+IN+(%27View%27)+AND+database+NOT+IN+(%27system%27%2C+%27information_schema%27%2C+%27INFORMATION_SCHEMA%27%2C+%27default%27)+AND+total_rows+%3E+0+ORDER+BY+database%2C+name+FORMAT+PrettyCompact"
  '
fi

echo "════════════════════════════════════════════════════════════════"
