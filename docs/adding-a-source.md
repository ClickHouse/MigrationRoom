# Adding a Migration Source

This guide explains how to add a new source database to the AI Migration Playground. The source can be a **local container** (PostgreSQL, ClickHouse OSS, MySQL) or a **cloud service** (Snowflake, BigQuery, Redshift, AlloyDB) — the playground supports both. After following this guide you will have a fully working migration scenario: an accessible source database, an MCP server the agent can talk to, a source-specific system prompt, and a step-by-step migration guide.

---

## How the playground is structured

Each migration source is self-contained under `sources/<source-name>/`:

```
sources/
├── postgres/                   ← PostgreSQL → ClickHouse Cloud  (local container)
│   ├── docker/                 ← Dockerfile + init SQL  (local sources only)
│   ├── queries/                ← sample OLAP queries + expected outputs
│   ├── scripts/                ← migration script + requirements.txt
│   ├── prompts/                ← ready-made prompts for each phase
│   └── GUIDE.md                ← step-by-step migration guide
│
├── clickhouse-oss/             ← ClickHouse OSS → ClickHouse Cloud  (local container)
│   ├── docker/init-data/       ← schema + seed SQL  (local sources only)
│   ├── queries/
│   ├── scripts/
│   ├── prompts/
│   └── GUIDE.md
│
└── snowflake/                  ← Snowflake → ClickHouse Cloud  (cloud source — no docker/)
    ├── queries/
    ├── scripts/
    ├── prompts/
    └── GUIDE.md
```

The agent's behaviour is controlled by a layered system prompt that is **assembled at build time** from three layers and injected into `librechat/librechat.yaml`. The full injection pipeline is explained in the [System prompt injection](#system-prompt-injection) section below.

---

## Step 1 — Create the source directory layout

```bash
mkdir -p sources/<source-name>/{queries,scripts,prompts}

# Local container sources only — skip for cloud services:
mkdir -p sources/<source-name>/docker/init-data
```

Minimum required files:

| File | Required for | Purpose |
|---|---|---|
| `sources/<source-name>/docker/` | Local container sources | Dockerfile and/or init SQL for seeding the source database |
| `sources/<source-name>/queries/sample_olap_queries.sql` | All sources | Representative analytical queries the migration exercise will optimise |
| `sources/<source-name>/scripts/migrate.py` | All sources | Data migration script (source → ClickHouse Cloud) |
| `sources/<source-name>/scripts/requirements.txt` | All sources | Python dependencies for the migration script |
| `sources/<source-name>/GUIDE.md` | All sources | Step-by-step migration guide for partners |

---

## Step 2 — Wire up the source database

### 2a — Local container source

Add two services to `docker-compose.yml`: the database container and an MCP server.

```yaml
services:

  # ── Source Database ──────────────────────────────────────────
  <source-name>:
    image: <database-image>
    ports:
      - "<host-port>:<container-port>"
    volumes:
      - ./sources/<source-name>/docker/init-data:/docker-entrypoint-initdb.d
      - <source-name>-data:/var/lib/<database-data-dir>
    healthcheck:
      test: ["CMD", "<healthcheck-command>"]
      interval: 5s
      timeout: 5s
      retries: 20
    networks:
      - playground-net

  # ── Source MCP Server ────────────────────────────────────────
  # supergateway wraps a stdio MCP server as an SSE endpoint for LibreChat.
  <source-name>-mcp:
    image: node:20-alpine
    command: >
      sh -c "npx -y supergateway --stdio 'npx -y <mcp-package>' --port 8000"
    environment:
      # Connection details for the MCP package — varies by package
      DATABASE_HOST: <source-name>
      DATABASE_PORT: "<port>"
    ports:
      - "<host-mcp-port>:8000"
    depends_on:
      <source-name>:
        condition: service_healthy
    networks:
      - playground-net

volumes:
  <source-name>-data:
```

> **Port allocation:** Postgres MCP uses host port `8001`, ClickHouse OSS MCP uses `8002`. Use the next available port (e.g. `8003`) for a third source to avoid conflicts.

Also add the new MCP service to LibreChat's `depends_on`:

```yaml
  librechat:
    depends_on:
      mongodb:
        condition: service_healthy
      postgres-mcp:
        condition: service_started
      clickhouse-oss-mcp:
        condition: service_started
      <source-name>-mcp:          # ← add this
        condition: service_started
```

### 2b — Cloud service source

For cloud databases (Snowflake, BigQuery, Redshift, AlloyDB, etc.) there is no container to spin up. The source already exists — you only need an MCP server that can reach it. Add a single service to `docker-compose.yml`:

```yaml
services:

  # ── Cloud Source MCP Server ──────────────────────────────────
  # Connects to the cloud service using credentials from .env.
  <source-name>-mcp:
    image: node:20-alpine
    command: >
      sh -c "npx -y supergateway --stdio 'npx -y <mcp-package>' --port 8000"
    environment:
      # Credentials injected from .env — never hardcode secrets here
      SNOWFLAKE_ACCOUNT: ${SNOWFLAKE_ACCOUNT}
      SNOWFLAKE_USER: ${SNOWFLAKE_USER}
      SNOWFLAKE_PASSWORD: ${SNOWFLAKE_PASSWORD}
      SNOWFLAKE_DATABASE: ${SNOWFLAKE_DATABASE}
      SNOWFLAKE_WAREHOUSE: ${SNOWFLAKE_WAREHOUSE}
    ports:
      - "<host-mcp-port>:8000"
    networks:
      - playground-net
```

Add the corresponding variables to `.env.example` so partners know what to fill in:

```bash
# ── Snowflake Source ──────────────────────────────────────────
# SNOWFLAKE_ACCOUNT=<org>-<account>
# SNOWFLAKE_USER=
# SNOWFLAKE_PASSWORD=
# SNOWFLAKE_DATABASE=
# SNOWFLAKE_WAREHOUSE=
```

Do **not** add the cloud MCP to LibreChat's `depends_on` with `service_healthy` — cloud MCP servers have no local healthcheck. Use `service_started` if you need to sequence the startup, or omit it entirely.

**Choosing an MCP package:**

| Source type | npm package | Notes |
|---|---|---|
| PostgreSQL (local) | `crystaldba/postgres-mcp` (Docker image) | Use `type: sse` directly — no supergateway needed |
| ClickHouse (local/cloud) | `mcp-clickhouse` | Wraps with supergateway |
| MySQL / MariaDB | `@benborla29/mcp-server-mysql` | Wraps with supergateway |
| MongoDB | `@modelcontextprotocol/server-mongodb` | Wraps with supergateway |
| SQLite | `@modelcontextprotocol/server-sqlite` | Wraps with supergateway |
| Snowflake | `@datawizardinc/mcp-snowflake-server` | Wraps with supergateway |
| BigQuery | `@ergut/mcp-bigquery-server` | Wraps with supergateway |
| Redshift | use Postgres MCP with Redshift endpoint | Standard psycopg2 connection |

> MCP package availability and names change frequently. Check [npmjs.com](https://www.npmjs.com) and [glama.ai/mcp/servers](https://glama.ai/mcp/servers) for the latest options before wiring up a new source.

---

## Step 3 — Register the MCP server in `librechat/librechat.yaml`

LibreChat discovers MCP servers from its config file. Add two things:

### 3a — Add the domain to `allowedDomains`

```yaml
mcpSettings:
  allowedDomains:
    - "postgres-mcp"
    - "clickhouse-oss-mcp"
    - "<source-name>-mcp"    # ← add this
```

### 3b — Add the MCP server entry under `mcpServers`

```yaml
mcpServers:
  <source-name>-source:
    type: sse
    url: "http://<source-name>-mcp:8000/sse"
    timeout: 60000
    serverInstructions: |
      This MCP server connects to the SOURCE <DatabaseName> database (<db-name>).
      Use it to explore schemas, run queries, and analyse data for migration.
      Access mode: read and write are both permitted.

      Key tables: <list key tables and their approximate row counts>
```

The `serverInstructions` field describes *this MCP server's* role and data. Migration rules go in the source-specific system prompt file — see Step 4.

> **Do not run `yq` manually** to update `librechat.yaml` — the build script only manages the `clickhouse-cloud` `serverInstructions`. All other edits to `librechat.yaml` are made directly.

---

## Step 4 — Write the source-specific system prompt

Create `librechat/sources/<source-name>-instructions.md`.

This file is automatically picked up by `scripts/build-instructions.sh` (it globs `librechat/sources/*.md`) and appended to the agent's system prompt alongside the base rules and ClickHouse best practices. Run `make setup` to rebuild and inject it.

### What to put in the file

Cover migration-relevant behaviours **specific to your source**. Do not repeat rules already in the base prompt (`librechat/clickhouse-cloud-instructions.md`) or in the ClickHouse best practices skill.

Every source file must open with a `##` heading that matches the label the build script
generates (`basename <file> | sed 's/-instructions.md//'`). This becomes the section
header in the assembled prompt:

```
---

## Source-Specific Rules: <source-name>

## <DatabaseName> → ClickHouse Cloud Migration   ← your file starts here
...
```

Typical sections:

```markdown
## <DatabaseName> → ClickHouse Cloud Migration

This section applies when the SOURCE database is <DatabaseName>.

---

### Data Type Mapping

How source-specific types map to ClickHouse:
- Enums         → LowCardinality(String) or Enum8/Enum16
- JSON / VARIANT → Map(String, String) or JSON (experimental)
- UUIDs         → String or FixedString(36)
- Arrays        → Array(T)
- TIMESTAMP_NTZ → DateTime or DateTime64(3)
- FLOAT / REAL  → Float64
- NUMBER(p, s)  → Decimal(p, s)
- <source type> → <ClickHouse equivalent>

---

### Migration Script Rules

Connection setup, chunking strategy, and type coercion rules the agent
should follow when generating or reviewing a migration script for this source.

For cloud sources: include how to authenticate (API key, OAuth, service account),
which Python library to use (e.g. snowflake-connector-python, google-cloud-bigquery),
and how to page through large result sets efficiently.

---

### Query Rewriting Notes

SQL dialect differences between the source and ClickHouse that partners will
encounter during the query rewriting phase (e.g. Snowflake QUALIFY, BigQuery
STRUCT access, Redshift LISTAGG).

---

### Known Gotchas

Edge cases that commonly cause migration failures for this source.
```

**Keep it focused.** The base prompt already handles: DDL idempotent forms, migration order (dimensions before facts), and MV backfill. Only add rules that are new or that override base behaviour for your source.

---

## Step 5 — Apply the changes

```bash
# Rebuild the system prompt and restart LibreChat
make setup
docker compose up -d
```

`make setup` runs `scripts/build-instructions.sh`, which re-assembles the agent system prompt and injects it into `librechat.yaml`. LibreChat picks up the new config on the next start.

Verify in LibreChat:
1. Click the MCP icon — `<source-name>-source` should appear in the list
2. Enable it and ask: *"List all tables in the \<db-name\> database"*

For cloud sources, confirm credentials are set in `.env` before running `make up`.

---

## System prompt injection

Understanding the injection pipeline helps when debugging unexpected agent behaviour.

### The three layers

The agent's `serverInstructions` for the `clickhouse-cloud` MCP entry is assembled from three layers in order:

```
Layer 1 — Base rules (generic, applies to all sources)
  librechat/clickhouse-cloud-instructions.md
  ↓
Layer 2 — Source-specific rules (one file per source, all concatenated)
  librechat/sources/postgres-instructions.md
  librechat/sources/clickhouse-oss-instructions.md
  librechat/sources/<source-name>-instructions.md
  ...
  ↓
Layer 3 — ClickHouse best practices (from agent-skills submodule)
  agent-skills/skills/clickhouse-best-practices/AGENTS.md
  ↓
Combined text → injected as serverInstructions
             for the clickhouse-cloud MCP in librechat/librechat.yaml
```

### The build script

`scripts/build-instructions.sh` does the following:

1. Reads `librechat/clickhouse-cloud-instructions.md` (Layer 1)
2. Globs `librechat/sources/*.md` alphabetically and concatenates all files (Layer 2)
3. Appends `agent-skills/skills/clickhouse-best-practices/AGENTS.md` (Layer 3)
4. Exports the combined string as `$YQ_VALUE`
5. Runs:
   ```bash
   yq -i '.mcpServers.clickhouse-cloud.serverInstructions = strenv(YQ_VALUE)' \
     librechat/librechat.yaml
   ```

The `serverInstructions` field in `librechat.yaml` is a **build artifact** — it is overwritten on every `make setup`. Never edit it directly; edit the source markdown files instead.

### Where the prompt ends up

LibreChat sends the `serverInstructions` content to the LLM as part of the system prompt when the `clickhouse-cloud` MCP is active. The agent sees all three layers as a single continuous block of text — there is no runtime layering.

**All source-specific rules are always active** — the agent sees rules for every source you have added, regardless of which source is currently in use. This is by design: it keeps the build simple (no runtime source detection). Scope each rule clearly (e.g. *"This section applies when the SOURCE is Snowflake"*) so the agent can self-select the relevant section.

### Debugging the assembled prompt

To inspect the current assembled prompt without restarting anything:

```bash
# Print the current serverInstructions value
yq '.mcpServers["clickhouse-cloud"].serverInstructions' librechat/librechat.yaml

# Check the character count after a rebuild
bash scripts/build-instructions.sh
```

---

## Checklist

Before opening a PR for a new source, verify:

- [ ] `sources/<source-name>/queries/sample_olap_queries.sql` — all queries run against the source
- [ ] `sources/<source-name>/scripts/migrate.py` — migrates data to a ClickHouse Cloud test service end-to-end
- [ ] `sources/<source-name>/GUIDE.md` — all phases work end-to-end with the AI agent
- [ ] `librechat/sources/<source-name>-instructions.md` — rules are generic (no hardcoded table/column names from the specific seed dataset or cloud account)
- [ ] `docker-compose.yml` — `make up` succeeds cleanly; MCP service starts and is reachable
- [ ] `librechat/librechat.yaml` — new MCP entry appears in LibreChat; agent can list tables
- [ ] `make setup` completes without errors after adding the new source file
- [ ] README updated to reference the new source in the migration sources table
- [ ] *(Local container sources only)* `sources/<source-name>/docker/` — database seeds correctly on `docker compose up`
- [ ] *(Cloud sources only)* `.env.example` — all required credential variables documented with placeholder values
