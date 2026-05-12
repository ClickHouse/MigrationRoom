.PHONY: setup up up-snowflake down reset reset-agent health logs pull diagram snowflake-setup snowflake-provision migration-status

setup:
	@echo "Setting up AI Migration Assistant..."
	@if [ ! -f "agent-skills/skills/clickhouse-best-practices/AGENTS.md" ]; then \
		echo "Cloning agent-skills from GitHub..."; \
		rm -rf agent-skills && git clone https://github.com/ClickHouse/agent-skills.git agent-skills; \
	fi
	@bash scripts/build-instructions.sh
	@if [ ! -f .env ]; then cp .env.example .env && echo "✅ .env created — add your LLM API key and ClickHouse Cloud credentials"; fi
	@echo "✅ Setup complete. Run: make up"

up:
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

up-snowflake:
	@echo "Pulling images..."
	docker compose --profile snowflake pull
	@echo "Building custom containers..."
	docker compose --profile snowflake build
	@echo "Starting services (including snowflake-source)..."
	docker compose --profile snowflake up -d
	@echo ""
	@echo "Container status:"
	@docker compose --profile snowflake ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
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

down:
	docker compose --profile snowflake down

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
