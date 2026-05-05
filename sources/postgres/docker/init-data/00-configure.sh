#!/bin/bash
# Passes DATASET_SIZE env var into Postgres as a session-persistent GUC
# so 02-seed.sql can read it with current_setting('app.dataset_size', true)
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    ALTER DATABASE "$POSTGRES_DB" SET "app.dataset_size" = '${DATASET_SIZE:-medium}';
EOSQL

echo "Dataset size configured: ${DATASET_SIZE:-medium}"
