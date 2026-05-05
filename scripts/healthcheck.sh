#!/usr/bin/env bash
# Verifies all playground services are reachable.
# Run via: make health
set -euo pipefail

PASS=0; FAIL=0

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

echo ""
echo "── AI Migration Playground — Health Check ────────────────"
check "Postgres (5432)"           "docker compose exec -T postgres pg_isready -U playground -d ecommerce"
# SSE endpoints: check HTTP 200 response code (stream body ignored)
check "Postgres MCP (8001/sse)"   "test \$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8001/sse) = 200"
check "ClickHouse MCP (8002/sse)" "test \$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8002/sse) = 200"
check "MongoDB (internal)"        "docker compose exec -T mongodb mongosh --eval 'db.adminCommand(\"ping\")' --quiet"
check "LibreChat (3080)"          "curl -sf --max-time 10 http://localhost:3080/"
# Streamable HTTP endpoint — just check TCP reachability (200 + any response body)
check "Docs MCP (remote)"         "curl -s --max-time 10 -w '%{http_code}' https://private-7c7dfe99.mintlify.app/mcp | grep -q '200\|405\|404'"
echo "──────────────────────────────────────────────────────────"
echo "  Result: ${PASS} passed, ${FAIL} failed"
echo ""
[ "$FAIL" -eq 0 ]
