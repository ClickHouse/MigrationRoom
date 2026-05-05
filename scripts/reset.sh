#!/usr/bin/env bash
# Tears down all containers and volumes, then rebuilds from scratch.
# WARNING: destroys all migrated data in ClickHouse and Postgres seed data.
set -euo pipefail

echo "⚠️  This will destroy all Docker volumes (Postgres data, ClickHouse data, MongoDB)."
echo "   Your .env and librechat.yaml will be preserved."
read -rp "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

docker compose down -v --remove-orphans
echo "Volumes removed. Rebuilding..."
docker compose up -d --build
echo ""
echo "✅ Playground reset. Postgres seed will run on first startup."
echo "   First run takes 5–10 min for medium dataset. Watch progress:"
echo "   docker compose logs postgres -f"
