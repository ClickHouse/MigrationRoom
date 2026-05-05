.PHONY: setup up down reset health logs pull diagram

setup:
	@echo "Setting up AI Migration Playground..."
	@if [ ! -f "agent-skills/skills/clickhouse-best-practices/AGENTS.md" ]; then \
		echo "Cloning agent-skills from GitHub..."; \
		rm -rf agent-skills && git clone https://github.com/ClickHouse/agent-skills.git agent-skills; \
	fi
	@bash scripts/build-instructions.sh
	@if [ ! -f .env ]; then cp .env.example .env && echo "✅ .env created — add your LLM API key and ClickHouse Cloud credentials"; fi
	@echo "✅ Setup complete. Run: make up"

up:
	docker compose up -d
	@echo ""
	@echo "Playground starting. Open http://localhost:3080 when ready."
	@echo "First run: allow 5–10 min for Postgres to seed (~10M rows)."
	@echo "Watch progress: docker compose logs postgres -f"

down:
	docker compose down

reset:
	@bash scripts/reset.sh

health:
	@bash scripts/healthcheck.sh

logs:
	docker compose logs -f --tail=50

pull:
	docker compose pull

diagram:
	@bash scripts/generate-diagram.sh
