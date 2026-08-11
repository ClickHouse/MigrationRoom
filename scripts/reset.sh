#!/usr/bin/env bash
# Tears down all containers and volumes, then rebuilds from scratch.
# WARNING: destroys all migrated data in ClickHouse and Postgres seed data.
#
# Every source (bundled or cloud) is Compose-profile-gated, so `docker
# compose down -v` on its own only sees whatever profile is currently
# active — it would silently leave other profiles' volumes (and
# containers) behind. `down` therefore always targets every profile via
# $ALL_PROFILES (passed in by `make reset`; falls back to the same literal
# list for a direct `bash scripts/reset.sh` run outside `make`), while the
# rebuild step preserves whatever profile the caller actually wants back
# ($COMPOSE_PROFILES if set, else the `make up` default of postgres +
# ClickHouse OSS).
set -euo pipefail

ALL_PROFILES="${ALL_PROFILES:-postgres,clickhouse-oss,snowflake,bigquery,databricks}"
UP_PROFILES="${COMPOSE_PROFILES:-postgres,clickhouse-oss}"

echo "⚠️  This will destroy all Docker volumes (Postgres data, ClickHouse data, MongoDB)."
echo "   Your .env and librechat.yaml will be preserved."
read -rp "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

COMPOSE_PROFILES="$ALL_PROFILES" docker compose down -v --remove-orphans
echo "Volumes removed. Rebuilding (profiles: ${UP_PROFILES})..."
COMPOSE_PROFILES="$UP_PROFILES" docker compose up -d --build
echo ""
echo "✅ Playground reset."
normalized_up=",$UP_PROFILES,"
if [[ "$normalized_up" == *,postgres,* ]]; then
    echo "   Postgres seed will run on first startup (~10M rows, 5–10 min)."
    echo "   Watch progress: docker compose logs postgres -f"
fi
if [[ "$normalized_up" == *,clickhouse-oss,* ]]; then
    echo "   ClickHouse OSS seed will run on first startup (~12.2M rows, 3–5 min)."
    echo "   Watch progress: docker compose logs clickhouse-oss -f"
fi
