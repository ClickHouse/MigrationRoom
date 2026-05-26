#!/usr/bin/env bash
# Delete the four pre-built migration agents from MongoDB and re-run
# librechat-init to recreate them. Use after changing AGENT_PROVIDER /
# AGENT_MODEL (or any AGENT_PROVIDER_<SOURCE>) in .env, since the init
# container's idempotency check skips agents that already match.
#
# Preserves: the playground user, conversations, source DB volumes.
set -euo pipefail

AGENTS=(
    "Postgres → ClickHouse Cloud"
    "Snowflake → ClickHouse Cloud"
    "BigQuery → ClickHouse Cloud"
    "ClickHouse OSS → ClickHouse Cloud"
)

echo "This will delete these agents from MongoDB:"
for a in "${AGENTS[@]}"; do echo "  - $a"; done
read -rp "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# Single regex-match delete so the operation is one round trip and works
# whether or not all four currently exist.
docker compose exec -T mongodb mongosh "mongodb://mongodb:27017/LibreChat" --quiet --eval '
    db.agents.deleteMany({
        name: { $in: [
            "Postgres → ClickHouse Cloud",
            "Snowflake → ClickHouse Cloud",
            "BigQuery → ClickHouse Cloud",
            "ClickHouse OSS → ClickHouse Cloud"
        ]}
    })
'

echo "✅ Agents deleted. Re-running librechat-init to recreate…"

# Auto-detect active profiles from currently-running services so the init
# container recreates the same agent set the partner is actually using.
# If they had `make up-snowflake` going, we want the Snowflake agent back.
# Caller can still override by exporting COMPOSE_PROFILES before invoking.
if [ -z "${COMPOSE_PROFILES:-}" ]; then
    detected=""
    running=$(docker compose ps --services --status running 2>/dev/null || true)
    if grep -q '^snowflake-source$' <<<"$running"; then
        detected="snowflake"
    fi
    if grep -q '^bigquery-source$' <<<"$running"; then
        detected="${detected:+$detected,}bigquery"
    fi
    export COMPOSE_PROFILES="$detected"
    echo "Detected active profiles: ${COMPOSE_PROFILES:-<none>}"
fi

docker compose run --rm librechat-init
