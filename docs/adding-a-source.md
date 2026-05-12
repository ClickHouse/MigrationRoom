# Adding a Migration Source

This guide explains how to add a new source database to the AI Migration Assistant. The source can be a **local container** (PostgreSQL, ClickHouse OSS, MySQL) or a **cloud service** (Snowflake, BigQuery, Redshift, AlloyDB) — the playground supports both. After following this guide you will have a fully working migration scenario: an accessible source database, an MCP server the agent can talk to, a source-specific system prompt, and a step-by-step migration guide.

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

> **Do not run `yq` manually** to update `librechat.yaml`'s injected blocks — `build-instructions.sh` manages everything below the `--- Migration Rules (auto-injected, do not edit below) ---` marker on each `<id>-source` MCP, plus the entirety of `clickhousectl.serverInstructions`. Hand-edit only the blurb above the marker on source MCPs, plus any non-`serverInstructions` fields.

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
1. The agent dropdown should now include **`<Source Display Name> → ClickHouse Cloud`**.
2. Pick it and ask: *"List all tables in the \<db-name\> database"* — `<source-name>-source` should be in the agent's attached MCPs (visible in the agent's tool panel) and respond.

For cloud sources, confirm credentials are set in `.env` before running `make up`.

> **Heads up:** adding a source also requires adding it to the `agents` array inside `docker-compose.yml`'s `librechat-init` `command:` block (so a per-source agent gets created) and to `librechat.yaml`'s `mcpSettings.allowedDomains` (so LibreChat will initialize the new MCP container). Run `make reset-agent` afterward to materialize the new agent.

---

## System prompt injection

Understanding the injection pipeline helps when debugging unexpected agent behaviour.

### How rules are routed

Each MCP server in `librechat.yaml` has its own `serverInstructions`. Per-source agents attach only their source's MCP + the shared target MCPs, so each agent receives only the rules it needs:

```
librechat/clickhouse-cloud-instructions.md  ─┐
agent-skills/.../AGENTS.md                  ─┴─→  mcpServers.clickhousectl.serverInstructions
                                                 (shared by all agents)

librechat/sources/postgres-instructions.md  ───→  mcpServers.postgres-source.serverInstructions
librechat/sources/snowflake-instructions.md ───→  mcpServers.snowflake-source.serverInstructions
librechat/sources/<id>-instructions.md      ───→  mcpServers.<id>-source.serverInstructions
```

### The build script

`scripts/build-instructions.sh` does the following on every `make setup`:

1. Sets `mcpServers.clickhousectl.serverInstructions` to `clickhouse-cloud-instructions.md` + `agent-skills/.../AGENTS.md` (fully build-managed).
2. For each `librechat/sources/<id>-instructions.md`, **appends** the file below a `--- Migration Rules (auto-injected, do not edit below) ---` marker in `mcpServers.<id>-source.serverInstructions`. The hand-edited MCP-purpose blurb above the marker is preserved; re-runs are idempotent. The build fails loudly if the matching `mcpServers.<id>-source` key is missing.

The text BELOW the marker is a **build artifact** — never edit it directly; edit the source markdown files instead.

### Where the prompt ends up

At chat time, LibreChat assembles the system prompt from the `serverInstructions` of every MCP attached to the agent. Because each per-source agent attaches only its own `<id>-source` MCP plus the shared `clickhousectl`, `clickhouse-docs`, and `migration-runner`, it sees only its own source's rules — no cross-source bleed. No agent-level `instructions` field is used.

Scope new rule files clearly (e.g. *"This section applies when the SOURCE is Snowflake"*) for readability, but the routing is structural — a Postgres agent never sees Snowflake rules.

### Debugging the assembled prompt

To inspect the current `serverInstructions` for any MCP without restarting anything:

```bash
# Shared target prompt (base + best-practices)
yq '.mcpServers["clickhousectl"].serverInstructions' librechat/librechat.yaml

# A source's prompt (blurb + auto-injected rules below the marker)
yq '.mcpServers["postgres-source"].serverInstructions' librechat/librechat.yaml
yq '.mcpServers["<id>-source"].serverInstructions' librechat/librechat.yaml

# Re-run the build (idempotent; per-source char counts are reported)
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
