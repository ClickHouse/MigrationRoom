#!/usr/bin/env bash
# Verifies all playground services are reachable.
#
# postgres, clickhouse-oss-mcp (and postgres-mcp) are Compose-profile-gated
# — after e.g. `make up-databricks` they are correctly NOT running. A check
# against an absent, profile-gated service is reported as SKIPPED, not
# FAILED, using the same "docker compose ps --services --status running"
# signal scripts/reset-agent.sh already uses to detect active profiles.
# snowflake-source / bigquery-source / databricks-mcp have been
# profile-gated all along but were never checked here in the first place,
# so there was nothing to fix for them.
#
# Run via: make health
set -euo pipefail

PASS=0; FAIL=0; SKIP=0

RUNNING_SERVICES="$(docker compose ps --services --status running 2>/dev/null || true)"

is_running() {
    grep -qx "$1" <<<"$RUNNING_SERVICES"
}

check() {
    local name="$1"; local cmd="$2"
    if eval "$cmd" &>/dev/null; then
        printf "  ✅ %-30s\n" "$name"
        PASS=$((PASS+1))
    else
        printf "  ❌ %-30s  FAILED\n" "$name"
        FAIL=$((FAIL+1))
    fi
}

# Like `check`, but for a service that's Compose-profile-gated: if
# `service` isn't part of the currently running project, report SKIPPED
# (profile inactive) instead of FAILED — that's the expected, correct
# state, not a health problem.
check_service() {
    local name="$1"; local service="$2"; local cmd="$3"
    if ! is_running "$service"; then
        printf "  ⏭️  %-30s  SKIPPED (profile inactive)\n" "$name"
        SKIP=$((SKIP+1))
        return
    fi
    check "$name" "$cmd"
}

echo ""
echo "── MigrationRoom — Health Check ────────────────"
check_service "Postgres (5432)"           "postgres"           "docker compose exec -T postgres pg_isready -U playground -d ecommerce"
# SSE endpoints: check HTTP 200 response code (stream body ignored)
check_service "Postgres MCP (8001/sse)"   "postgres-mcp"       "test \$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8001/sse) = 200"
check_service "ClickHouse MCP (8002/sse)" "clickhouse-oss-mcp" "test \$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8002/sse) = 200"
check "MongoDB (internal)"        "docker compose exec -T mongodb mongosh --eval 'db.adminCommand(\"ping\")' --quiet"
check "LibreChat (3080)"          "curl -sf --max-time 10 http://localhost:3080/"
# Streamable HTTP endpoint — just check TCP reachability (200 + any response body)
check "Docs MCP (remote)"         "curl -s --max-time 10 -w '%{http_code}' https://private-7c7dfe99.mintlify.app/mcp | grep -q '200\|405\|404'"
echo "──────────────────────────────────────────────────────────"
echo "  Result: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped"
echo ""
[ "$FAIL" -eq 0 ]
