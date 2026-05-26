.PHONY: setup up up-snowflake up-bigquery down reset reset-agent health logs pull diagram snowflake-setup snowflake-provision bigquery-provision tpch-data tpch-load-bigquery tpch-load-postgres tpch-load-clickhouse-oss migration-status

setup:
	@echo "Setting up MigrationHouse..."
	@if [ ! -f "agent-skills/skills/clickhouse-best-practices/AGENTS.md" ]; then \
		echo "Cloning agent-skills from GitHub..."; \
		rm -rf agent-skills && git clone https://github.com/ClickHouse/agent-skills.git agent-skills; \
	fi
	@bash scripts/build-instructions.sh
	@if [ ! -f .env ]; then cp .env.example .env && echo "✅ .env created — add your LLM API key and ClickHouse Cloud credentials"; fi
	@# Ensure ./secrets/ exists and has a placeholder gcp-key.json so docker
	@# compose can always bind-mount it into bigquery-source / migration-runner.
	@# The placeholder is harmless when BigQuery isn't in use; partners using
	@# BigQuery replace it (or `make bigquery-provision` writes the real key here).
	@mkdir -p secrets && [ -f secrets/gcp-key.json ] || echo '{}' > secrets/gcp-key.json
	@# Seed a runtime librechat.yaml so `docker compose up` works even
	@# before the user has run one of the up* targets. Defaults to empty
	@# profiles, which strips snowflake-source and bigquery-source.
	@COMPOSE_PROFILES="" bash scripts/build-librechat-runtime.sh
	@echo "✅ Setup complete. Run: make up"

up: export COMPOSE_PROFILES :=
up:
	@echo "Regenerating librechat.runtime.yaml for active profiles: <none>"
	@bash scripts/build-librechat-runtime.sh
	@echo "Pulling images..."
	docker compose pull
	@echo "Building custom containers..."
	docker compose build
	@echo "Starting services..."
	docker compose up -d
	@echo ""
	@echo "Container status:"
	@docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
	@echo ""
	@echo "Open http://localhost:3080 when LibreChat shows '(healthy)'."
	@echo "First run: allow 5–10 min for Postgres to seed (~10M rows)."
	@echo "Watch seed: docker compose logs postgres -f"

up-snowflake: export COMPOSE_PROFILES := snowflake
up-snowflake:
	@echo "Regenerating librechat.runtime.yaml for active profiles: snowflake"
	@bash scripts/build-librechat-runtime.sh
	@echo "Pulling images..."
	docker compose pull
	@echo "Building custom containers..."
	docker compose build
	@echo "Starting services (including snowflake-source)..."
	docker compose up -d
	@echo ""
	@echo "Container status:"
	@docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
	@echo ""
	@echo "If snowflake-source shows unhealthy, check: docker compose logs snowflake-source"
	@echo "(SNOWFLAKE_* credentials in .env must be set and valid.)"

snowflake-setup:
	@echo "Installing setup dependencies (snowflake-connector-python, etc.)…"
	@python3 -m pip install --quiet -r sources/snowflake/scripts/requirements.txt
	@echo "Setting up MIGRATION_DEMO workload in Snowflake (Path A)…"
	@python3 sources/snowflake/scripts/setup_workload.py

snowflake-provision:
	@echo "Provisioning Snowflake demo environment with Terraform (Path B)…"
	cd sources/snowflake/terraform && terraform init && terraform apply
	@echo ""
	@echo "Capture the .env block with: cd sources/snowflake/terraform && terraform output -raw env_block"

up-bigquery: export COMPOSE_PROFILES := bigquery
up-bigquery:
	@echo "Regenerating librechat.runtime.yaml for active profiles: bigquery"
	@bash scripts/build-librechat-runtime.sh
	@echo "Pulling images..."
	docker compose pull
	@echo "Starting services (including bigquery-source)..."
	docker compose up -d
	@echo ""
	@echo "Container status:"
	@docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
	@echo ""
	@echo "If bigquery-source shows unhealthy, check: docker compose logs bigquery-source"
	@echo "(BIGQUERY_PROJECT and BIGQUERY_KEY_FILE in .env must be set and valid.)"

bigquery-provision:
	@echo "Provisioning BigQuery demo environment with Terraform (Path B)…"
	cd sources/bigquery/terraform && terraform init && terraform apply
	@echo ""
	@echo "Capture the .env block with: cd sources/bigquery/terraform && terraform output -raw env_block"

# Shared TPC-H workload. BigQuery is the first loader; future sources
# get sibling targets (tpch-load-postgres, tpch-load-clickhouse-oss).
# The Snowflake source keeps `snowflake-setup` — different mechanics
# (SNOWFLAKE_SAMPLE_DATA copy), same end-state.
tpch-data:
	@echo "Generating TPC-H SF1 .tbl files in workloads/tpch/data/sf1/…"
	docker build -t tpch-dbgen workloads/tpch/dbgen
	docker run --rm -v $(PWD)/workloads/tpch/data:/data tpch-dbgen 1

tpch-load-bigquery: tpch-data
	@echo "Installing loader dependencies (google-cloud-bigquery, db-dtypes)…"
	@python3 -m pip install --quiet -r workloads/tpch/bigquery/requirements.txt
	@echo "Loading TPC-H SF1 + augmentations into BigQuery…"
	@# google-cloud-bigquery reads GOOGLE_APPLICATION_CREDENTIALS, but
	@# .env carries BIGQUERY_KEY_FILE — map across so partners don't
	@# have to remember the dual env-var contract.
	@set -a; [ -f .env ] && . ./.env; set +a; \
	  : "$${GOOGLE_APPLICATION_CREDENTIALS:=$$(cd "$$(dirname "$${BIGQUERY_KEY_FILE:-./secrets/gcp-key.json}")" 2>/dev/null && pwd)/$$(basename "$${BIGQUERY_KEY_FILE:-./secrets/gcp-key.json}")}"; \
	  export GOOGLE_APPLICATION_CREDENTIALS; \
	  python3 workloads/tpch/bigquery/load.py

# Postgres + ClickHouse OSS loaders create a NEW `tpch` database
# alongside the bundled e-commerce / web-analytics workloads, so
# partners can switch between demos by toggling POSTGRES_DB / CH_OSS_DB.
# These targets assume the bundled containers are running (`make up`).
# Partner POSTGRES_HOST in .env is typically unset or `postgres`; the
# loader runs on the host, so override to `localhost` for the apply.
tpch-load-postgres: tpch-data
	@echo "Installing loader dependencies (psycopg2-binary)…"
	@python3 -m pip install --quiet -r workloads/tpch/postgres/requirements.txt
	@echo "Loading TPC-H SF1 + augmentations into Postgres (database=tpch)…"
	@set -a; [ -f .env ] && . ./.env; set +a; \
	  POSTGRES_HOST="$${POSTGRES_HOST_LOCAL:-localhost}" \
	  POSTGRES_DB=tpch \
	  python3 workloads/tpch/postgres/load.py

tpch-load-clickhouse-oss: tpch-data
	@echo "Installing loader dependencies (clickhouse-connect)…"
	@python3 -m pip install --quiet -r workloads/tpch/clickhouse-oss/requirements.txt
	@echo "Loading TPC-H SF1 + augmentations into ClickHouse OSS (database=tpch)…"
	@set -a; [ -f .env ] && . ./.env; set +a; \
	  CH_OSS_HOST="$${CH_OSS_HOST_LOCAL:-localhost}" \
	  CH_OSS_DB=tpch \
	  python3 workloads/tpch/clickhouse-oss/load.py

down:
	docker compose --profile snowflake --profile bigquery down

reset:
	@bash scripts/reset.sh

reset-agent:
	@bash scripts/reset-agent.sh

health:
	@bash scripts/healthcheck.sh

migration-status:
	@bash scripts/migration-status.sh

logs:
	docker compose logs -f --tail=50

pull:
	docker compose pull

diagram:
	@bash scripts/generate-diagram.sh
