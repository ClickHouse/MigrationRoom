# Migration Guide — PostgreSQL → ClickHouse Cloud

This guide walks you through a complete Postgres → ClickHouse migration using the AI agent in LibreChat. Each step is designed to be completed with AI assistance — the agent has live MCP connections to both databases.

**Total time:** ~2.5 hours (all 5 phases)  
**Prompts:** Ready-made prompts for each step are in [prompts/](prompts/)

---

## Phase 1 — Environment Setup (15 min)

### Step 1: Enable the ClickHouse Cloud MCP server

Before starting, enable the remote MCP server for your ClickHouse Cloud service:

1. Open the [ClickHouse Cloud console](https://clickhouse.cloud)
2. Select your service → click **Connect**
3. Choose the **MCP** tab
4. Toggle **Enable remote MCP server** on

This is a one-time step per service. See the [official docs](https://clickhouse.com/docs/cloud/features/ai-ml/remote-mcp#enabling) for screenshots.

### Step 2: Clone and configure

```bash
git clone https://github.com/ClickHouse/ai-migration-assistant.git
cd ai-migration-assistant
make setup
```

Edit `.env` — add your LLM API key and ClickHouse Cloud credentials:
```bash
ANTHROPIC_API_KEY=sk-ant-...        # or OPENAI_API_KEY=sk-...
CLICKHOUSE_CLOUD_HOST=<your-service>.clickhouse.cloud
CLICKHOUSE_CLOUD_USER=default
CLICKHOUSE_CLOUD_PASSWORD=<your-password>
```

### Step 3: Launch the playground

```bash
make up
# First run: Postgres seeds ~10M rows — allow 5–10 minutes
docker compose logs postgres -f   # watch seed progress
```

### Step 4: Open LibreChat

Navigate to **http://localhost:3080**. Sign in (`admin@playground.local` / `playground`). Select your preferred model.

### Step 5: Enable MCP servers

Click the **MCP** icon in the chat toolbar and enable:
- `postgres-source` — source PostgreSQL database
- `clickhouse-cloud` — target ClickHouse Cloud (OAuth login required on first use)
- `clickhouse-cloud-write` — write access to ClickHouse Cloud (DDL + INSERT)
- `clickhouse-docs` — ClickHouse documentation

Test connections:
> "What tables are in the ecommerce Postgres database, and what databases exist in ClickHouse Cloud?"

---

## Phase 2 — Schema Discovery and Analysis (30 min)

### Step 6: Explore the source schema

Use prompt [01-discover-schema.md](prompts/01-discover-schema.md) or ask:
> "Explore the ecommerce database schema on Postgres — tables, row counts, column types, and indexes."

### Step 7: Analyse query patterns

Open `queries/sample_olap_queries.sql`, paste the contents, then ask:
> "What do these queries tell us about the ideal ORDER BY keys for the ClickHouse schema?"

### Step 8: Identify migration challenges

> "What Postgres-specific features in this schema need special handling in ClickHouse?"

### Step 9: Generate a Migration Planning Report

> "Generate a migration planning report."

LibreChat will open an **artifact side panel** with a rendered HTML report. Use the **Download** button to save and share with stakeholders before proceeding.

---

## Phase 3 — Schema Creation in ClickHouse (45 min)

### Step 10: Design the target schema

Use prompt [03-design-schema.md](prompts/03-design-schema.md) or ask:
> "Design an optimised ClickHouse schema for migration_target."

### Step 11: Challenge the design

> "Are the ORDER BY key choices optimal for our query patterns? What tradeoffs should we consider?"

### Step 12: Execute schema creation

> "Create the migration_target schema in ClickHouse Cloud."

The agent uses the `clickhouse-cloud-write` MCP to execute DDL directly — no copy-pasting required.

---

## Phase 4 — Data Migration (45 min)

### Step 13: Generate the migration script

> **Lab vs production:** In production, use [ClickPipes](https://clickhouse.com/docs/integrations/clickpipes) or load from S3. Here, Postgres runs inside Docker and isn't reachable from ClickHouse Cloud directly, so we use a Python script run locally.

Ask:
> "Generate a Python migration script to move data from Postgres to ClickHouse Cloud."

The script handles dimension-first ordering, monthly batching for large tables, and type coercion for JSONB, arrays, and ENUMs.

### Step 14: Verify the target schema

> "Verify the target schema is ready before we start migrating."

### Step 15: Run the migration

```bash
pip install -r sources/postgres/scripts/requirements.txt
source .env
python3 sources/postgres/scripts/migrate.py
```

### Step 16: Validate row counts

> "Compare row counts between source and target. Flag any discrepancies."

### Step 17: Generate a Post-Migration Report

> "Generate a post-migration report."

LibreChat opens an artifact with a row-count comparison table, a full object mapping, and a findings section covering type coercions and any anomalies.

---

## Phase 5 — Query Rewriting and Optimisation (30 min)

### Step 18: Rewrite queries

Use prompt [05-rewrite-queries.md](prompts/05-rewrite-queries.md) or ask:
> "Rewrite the sample queries for ClickHouse and explain any changes."

### Step 19: Compare performance

> "Run a sample query on both Postgres and ClickHouse and compare the EXPLAIN output and timing."

### Step 20: Create Materialized Views

Use prompt [06-optimize.md](prompts/06-optimize.md) or ask:
> "Propose Materialized View optimisations for the heaviest aggregation queries."

---

## Validation

When complete, compare results against the reference solutions:

- **Schema:** `queries/expected_ch_schema.sql`
- **Queries:** `queries/expected_ch_queries.sql`
- **Checklist:** `MIGRATION_CHECKLIST.md`

Run prompt [07-validate.md](prompts/07-validate.md) to cross-check with the expected outputs.
