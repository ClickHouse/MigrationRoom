#!/usr/bin/env bash
# Delete the three pre-built migration agents from MongoDB and re-run
# librechat-init to recreate them. Use after changing AGENT_PROVIDER /
# AGENT_MODEL (or any AGENT_PROVIDER_<SOURCE>) in .env, since the init
# container's idempotency check skips agents that already match.
#
# Preserves: the playground user, conversations, source DB volumes.
set -euo pipefail

AGENTS=(
    "Postgres → ClickHouse Cloud"
    "Snowflake → ClickHouse Cloud"
    "ClickHouse OSS → ClickHouse Cloud"
)

echo "This will delete these agents from MongoDB:"
for a in "${AGENTS[@]}"; do echo "  - $a"; done
read -rp "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# Single regex-match delete so the operation is one round trip and works
# whether or not all three currently exist.
docker compose exec -T mongodb mongosh "mongodb://mongodb:27017/LibreChat" --quiet --eval '
    db.agents.deleteMany({
        name: { $in: [
            "Postgres → ClickHouse Cloud",
            "Snowflake → ClickHouse Cloud",
            "ClickHouse OSS → ClickHouse Cloud"
        ]}
    })
'

echo "✅ Agents deleted. Re-running librechat-init to recreate…"
docker compose run --rm librechat-init
