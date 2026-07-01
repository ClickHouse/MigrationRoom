# Adding a Migration Source

This guide explains how to add a new source database to the MigrationRoom. The source can be a **local container** (PostgreSQL, ClickHouse OSS, MySQL) or a **cloud service** (Snowflake, BigQuery, Redshift, AlloyDB) — the playground supports both. After following this guide you will have a fully working migration scenario: an accessible source database, an MCP server the agent can talk to, a source-specific system prompt, and a step-by-step migration guide.

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

## Architecture overview

The playground is a Docker Compose stack of ~13 services plus a few external dependencies. The diagram below is the single source of truth for how the pieces fit together.

![MigrationRoom architecture](architecture.png)

> Regenerate after editing `docs/architecture.mmd`: `make diagram` (uses `mermaid-cli` via `npx`).

### Component map

The components fall into five groups:

**Entry + UI layer:**

- **`nginx`** — HTTPS terminator on `:443`. Three location blocks: `/dashboard/*` → `migration-dashboard`, `/api/mk/*` → `migration-runner`'s FastAPI, `/` → `librechat`. The HTTPS cert is self-signed and required by Sandpack's `crypto.subtle` (artifact rendering).
- **`migration-dashboard`** — React SPA served by nginx-alpine. Lives under [docker/migration-dashboard/](../docker/migration-dashboard/). Talks to the migration-runner's FastAPI (REST for snapshots, SSE for live events) and to MongoDB (read-only via the runner) for LibreChat conversation lookup.
- **`librechat` + `mongodb`** — the chat UI. MongoDB stores conversations, messages, and pre-built agents created by the `librechat-init` one-shot container.

**Agent runtime — Python sandbox + run state:**

- **`migration-runner`** — multi-purpose service. Hosts:
  - **MCP server** on SSE: exposes `run_python`, `run_python_background`, `tail_python_job`, `write_workspace_file`, `read_workspace_file`, `list_workspace_files`. This is the agent's Python sandbox.
  - **FastAPI** on `:8001`: REST + SSE for the dashboard. Endpoints under `/api/mk/runs/*` and `/api/mk/sources/*`. Source manifests and conversation pre-creation live here too.
  - **`migrationkit` Python library** (importable inside `run_python`): `Migrator`, `Validator`, `Benchmarker`, the `Source` ABC (with concrete `PostgresSource`, `SnowflakeSource`, `BigQuerySource`, `ClickHouseOssSource`), `ClickHouseTarget`, and `S3Stage` / `GCSStage` for object-storage staging paths.
  - **SQLite WAL state store** at `/workspace/state/migrationkit.db`. Authoritative state across the stack: `runs`, `run_tables`, `events`, `batches`, `controls`, `validations`, `benchmarks`. Concurrent writers in the same container; readers via FastAPI.

**Source MCP layer** — one MCP server per supported source, all exposed to LibreChat over SSE:

- **`postgres-mcp`** (`crystaldba/postgres-mcp`) — `execute_sql`.
- **`clickhouse-oss-mcp`** (`mcp-clickhouse`) — `run_select_query`.
- **`snowflake-source`** (`snowflake-labs/mcp`) + **`snowflake-source-shim`** — the shim is a Python proxy that strips JSON-Schema fields (`exclusiveMaximum`, `const`, `oneOf`, `allOf`, `$schema`) that Gemini's function-calling API rejects. LibreChat connects to the shim, not the upstream MCP.
- **`bigquery-source`** (Google MCP toolbox) — `bigquery-list-dataset-ids`, `bigquery-list-table-ids`, `bigquery-get-table-info`, plus `INFORMATION_SCHEMA` access.

**Target MCP — write-enabled:**

- **`clickhousectl-mcp`** — the official `mcp-clickhouse` image with `CLICKHOUSE_ALLOW_WRITE_ACCESS=true` and `clickhouse-client` installed. The agent uses this for DDL (`CREATE TABLE`, `CREATE MATERIALIZED VIEW`) and ad-hoc verification queries. The managed `mcp.clickhouse.cloud` MCP is read-only and can't issue DDL — that's why a local write-enabled copy exists.

**Bundled source databases** (run inside the same Docker Compose):

- **`postgres`** — PostgreSQL 16 with `ecommerce` (~10M rows) and optional `tpch` (SF1) datasets.
- **`clickhouse-oss`** — ClickHouse OSS with `analytics` (web events, ~12.2M rows) and optional `tpch` (SF1) datasets.

Snowflake and BigQuery don't have bundled databases — the source MCPs connect to partner-provided cloud accounts.

---

## `migration-runner` — Python sandbox + run state

`migration-runner` is the most architecturally important service. It's both the agent's Python sandbox AND the source of truth for run state. Lives under [docker/migration-runner/](../docker/migration-runner/).

### MCP tools

Exposed via SSE to LibreChat. All four agents attach this MCP.

| Tool | Use for |
|---|---|
| `run_python(code, timeout_seconds=3600)` | Synchronous Python execution. Blocks until the script exits. Use ONLY for short scripts (<60 s) — schema checks, validation queries, small inserts. Output appears only at exit. |
| `run_python_background(code, timeout_seconds=3600)` | Launches Python in the background. Returns `{job_id, pid}` immediately. The agent then issues exactly **ONE** `tail_python_job` to confirm `status=running` and stops. |
| `tail_python_job(job_id, stdout_offset, stderr_offset, max_wait_seconds=60, min_chunk_seconds=30)` | Returns the stdout/stderr delta since the supplied offsets + the run's current status. **The agent calls this exactly once per dispatch — not in a loop.** |
| `write_workspace_file(path, content)` | Writes a file under `/workspace/`. The agent uses this to persist the migration script before dispatching it. |
| `read_workspace_file(path)` / `list_workspace_files()` | Read-only file ops on `/workspace/`. |

**Why dispatch + ONE tail instead of polling:** every `tail_python_job` call replays the full conversation context to the LLM — repeated polling burns tokens quadratically with no UX benefit. The dashboard's Migration tab already streams per-table progress via SSE; the agent's job is to dispatch + stop, and the partner watches the dashboard. This is enforced in every source's `sources/<id>/prompts/02-migrate-data.md` and in the system prompts at `librechat/sources/{snowflake,bigquery}-instructions.md`.

### FastAPI HTTP surface

Served at `:8001` (proxied through nginx as `/api/mk/*`). Key endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/runs` | List all runs (used by the dashboard's run picker) |
| `GET /api/runs/{run_id}` | One run + its `run_tables` rows (snapshot for the Migration tab) |
| `GET /api/runs/{run_id}/events` | **Server-Sent Events stream** of every event written for this run |
| `GET /api/runs/{run_id}/validations` | Per-table validation rows (Validation tab) |
| `GET /api/runs/{run_id}/benchmarks` | Per-query benchmark rows (Benchmark tab) |
| `POST /api/runs/{run_id}/pause` / `/resume` / `/cancel` | Run control. The `Migrator` polls a `controls` row each batch boundary. |
| `POST /api/runs/{run_id}/mark/{step}` | Legacy step-marker (still works for non-migrationkit agents). |
| `GET /api/sources` | List source manifests from `sources/<id>/manifest.json` |
| `GET /api/sources/{src}/databases` | List source databases (used by the SETUP card's source-database dropdown) |
| `POST /api/sources/{src}/conversation` | Pre-create a LibreChat conversation bound to the source's agent. **This is how source-switching in the SETUP card auto-switches the LibreChat agent** — see "MongoDB conversation pre-creation" below. |

### SQLite state schema

WAL-mode SQLite at `/workspace/state/migrationkit.db`. Tables:

- **`runs`** — `run_id`, `source_type`, `source_database`, `target_database`, `status` (running/done/paused/cancelled/failed), `started_at`, `ended_at`, `error`.
- **`run_tables`** — per-table progress for one run: `table_name`, `total_rows`, `rows_done`, `status` (pending/running/done/error), `strategy` (direct/s3_stage/gcs_stage), `phase` (unloading/staged/loading/validating for staged paths).
- **`batches`** — per-batch durability for resume-on-pause; row counts and byte deltas per batch insert.
- **`events`** — append-only event log. Every dashboard view derives from this. Event kinds include `started`, `table_done`, `batch_done`, `bytes_progress`, `phase_started`, `paused`, `resumed`, `cancelled`, `log`, `validation_row`, `validation_done`, `step_validated`, `benchmark_row`, `benchmark_done`, `step_benchmarked`. The FastAPI SSE endpoint streams this table.
- **`controls`** — one row per active run with `requested_state` (pause/resume/cancel). `Migrator._migrate_table()` polls this row at every batch boundary.
- **`validations`** — per-`(run_id, table_name)` row count comparison written by `Validator.validate()`.
- **`benchmarks`** — per-`(run_id, query_n)` source/target timing written by `Benchmarker.benchmark()`.

### `migrationkit` library

The Python library inside the runner that handles data movement. Critical pieces:

```python
from migrationkit import (
    Migrator, Validator, Benchmarker,
    PostgresSource, SnowflakeSource, BigQuerySource, ClickHouseOssSource,
    ClickHouseTarget,
    S3Stage, GCSStage,
)
```

- **`Migrator(run_id, source, target, *, target_database=None)`** — orchestrates a multi-table migration. Calls `target.use_database(target_database)` in `__init__` so every read (`count_rows`) and write (`insert_batch`, `load_from_s3`, `load_from_gcs`) resolves against the run's database, not `CLICKHOUSE_CLOUD_DATABASE` from env. Per-table batch checkpointing, pause/resume/cancel responsiveness, structured event emission to SQLite.
- **`Validator(run_id, source, target, *, target_database=None).validate()`** — row count parity per table from the run's `run_tables`. Writes to `validations` + emits `validation_row` / `validation_done` / `step_validated` events.
- **`Benchmarker(run_id, source, target, *, target_database=None).benchmark(queries=[...])`** — per-query timing. Each `Source.execute_and_count(sql)` returns `(rows, server_ms, wall_ms)`. Server-side timing is sourced from each engine's native surface (`X-ClickHouse-Summary.elapsed_ns`, `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY.EXECUTION_TIME`, `EXPLAIN ANALYZE.Execution Time`, `QueryJob.ended - QueryJob.started`).
- **`Source` ABC** — the contract for adding a new source. Methods: `count_rows(sql)`, `iter_batches(sql, batch_size)`, `execute_and_count(sql)`, `unload_to_s3(sql, stage)` or `unload_to_gcs(sql, stage)` (optional, for staging paths), `close()`.
- **`ClickHouseTarget`** — write target. `use_database(db)` is the propagation hook — updates `self.database`, `self._client.database`, and `_conn_params["database"]` together so bare-name inserts (`client.insert(table, data)`) resolve against the run's database. `load_from_s3` / `load_from_gcs` issue `INSERT INTO <db>.<table> SELECT * FROM s3(...)` / `gcs(...)` with progress polling via `system.processes`.
- **`S3Stage` / `GCSStage`** — credentialed handles for the object-storage staging paths. `from_env()` reads `STAGING_S3_*` / `STAGING_GCS_*` env vars.

### Object-storage staging paths

For large fact tables, `migrationkit` supports a "stage and import" path that bypasses the per-row HTTPS insert path:

- **Snowflake → S3 → ClickHouse Cloud**: `COPY INTO @stage` on Snowflake → `INSERT FROM s3('<glob>', '<key>', '<secret>', 'Parquet')` on the target.
- **BigQuery → GCS → ClickHouse Cloud**: `EXPORT DATA` on BigQuery → `INSERT FROM gcs('<glob>', '<hmac_key>', '<hmac_secret>', 'Parquet')` on the target. (ClickHouse's `gcs()` table function requires HMAC keys, not service-account ADC.)
- **ClickHouse OSS → S3 → ClickHouse Cloud**: `INSERT INTO FUNCTION s3(...)` on the source → `INSERT FROM s3(...)` on the target.

Each path emits `phase_started` events (`unloading` → `staged` → `loading` → `validating`) so the dashboard can render multi-phase progress for large tables. The agent picks the path per-table based on row count and whether the relevant `STAGING_*` env vars are set.

---

## `migration-dashboard` — React SPA + live event stream

React + Vite SPA served by nginx-alpine. Lives under [docker/migration-dashboard/](../docker/migration-dashboard/).

### Component layout

The dashboard renders five regions stacked top-to-bottom in the left pane (the right pane is LibreChat in an iframe):

- **Chrome** (`Chrome.tsx`) — top bar with logomark, status pill, user identity.
- **Setup** (`Setup.tsx`) — Source dropdown, Source-database dropdown, OLAP queries editor. Changing the Source dropdown calls `POST /api/sources/{src}/conversation` (pre-create the agent's conversation) and updates the iframe `src` to point at it.
- **Conversation** (`ConversationPicker.tsx`) — current/past LibreChat conversations for the active source.
- **Steps** (`StepButtons.tsx`) — the six step buttons. Clicking a button does two things: (1) read the corresponding `sources/<src>/prompts/0X-*.md` text via the runner's FastAPI; (2) inject the text into the right-pane LibreChat conversation via `postMessage` to the iframe (LibreChat's iframe is same-origin under HTTPS, so this works).
- **LiveRun** (`LiveRun.tsx`) — the KPI dashboard. Tabbed: Migration / Validation / Benchmark. Each tab is its own component (`MigrationView`, `ValidationView`, `BenchmarkView`) and consumes a hook (`useLiveRun`, `useValidations`, `useBenchmarks`) that subscribes to the SSE stream.

### SSE subscription model

Three hooks open EventSources against `/api/mk/runs/{run_id}/events`:

- **`useLiveRun(run_id)`** — subscribes to migration-flavoured events (`started`, `table_done`, `batch_done`, `bytes_progress`, `phase_started`, `run_done`, etc). On each event it updates an in-memory snapshot of `runs` + `run_tables` for the Migration tab.
- **`useValidations(run_id)`** — listens for `validation_row` / `validation_done` / `step_validated` and refetches `/api/mk/runs/{id}/validations`.
- **`useBenchmarks(run_id)`** — listens for `benchmark_row` / `benchmark_done` / `step_benchmarked` and refetches `/api/mk/runs/{id}/benchmarks`.

The hooks tolerate unknown event kinds gracefully — adding a new event in the runner is purely additive. An `onerror` handler bails after 5 consecutive errors so a dead server doesn't trigger an indefinite reconnect storm.

### Auto-agent-selection via MongoDB conversation pre-creation

LibreChat v0.8.5 doesn't honor `?endpoint=agents&agent_id=...` URL params at conversation load. To make the SETUP card's Source dropdown auto-switch the right-pane agent, the runner's `POST /api/sources/{src}/conversation` endpoint patches MongoDB directly:

1. Look up the agent's `agent_id` by name (e.g. `Postgres → ClickHouse Cloud`) via `mongosh` `findOne` on the `agents` collection.
2. Insert (or update) a `conversations` document with `endpoint=agents`, `agentOptions={ agent: <agent_id> }`, and the playground user as `user`.
3. Insert a `messages` document for the initial system message so the conversation isn't empty.
4. Return the `conversationId` to the dashboard.

The dashboard then sets the iframe `src` to `https://localhost/c/<conversationId>` and LibreChat picks it up. This is also why the step buttons can write to the LibreChat input via `postMessage` — same origin, same auth cookie.

### Source manifest

Each source has a `sources/<id>/manifest.json` with display metadata (name, logo, source-type for the agent picker). The runner reads it on `GET /api/sources` and the dashboard uses it to populate the Source dropdown — so adding a new source's directory automatically surfaces it in the UI as long as the manifest is present.

---

## Workload design — read this before Step 1

A migration demo is only convincing if the workload is realistic and forces
the AI agent to make non-trivial decisions. Pick the workload deliberately
before you write any code.

### Prefer reusing the existing TPC-H workload

The Snowflake source already ships a TPC-H workload augmented with five
Snowflake-specific features. **If your new source is a SQL warehouse / OLTP
database / OLAP engine, reuse it.** Concretely:

- The 8 TPC-H tables (`region`, `nation`, `supplier`, `customer`, `part`,
  `partsupp`, `orders`, `lineitem`) cover dimensions + facts + a typical
  join graph. Total ~6 M rows at scale factor 1 — big enough for streaming
  progress, small enough for a 10–15 minute migration.
- The Snowflake augmentations are documented in
  `sources/snowflake/scripts/setup_workload.sql`: a `VARIANT` column on
  `ORDERS`, a `TIMESTAMP_TZ` column on `LINEITEM`, a `CLUSTER BY` clause,
  a `STREAM`, and a `DYNAMIC TABLE`. For your new source, swap each of
  these for the source's equivalent feature (or its closest analog).
- Reusing TPC-H means partners can compare migrations side-by-side across
  sources — same tables, same shapes, only the source-specific decoration
  changes.

Two existing sources predate this convention and use bespoke workloads
(Postgres → e-commerce; ClickHouse OSS → web analytics). They are NOT being
refactored — but new sources should follow the TPC-H pattern.

### Required properties of any workload

Whether you reuse TPC-H or build something parallel for a non-SQL source,
the workload **must**:

1. **Have fact + dimension structure.** A single flat table is not a
   migration; it's a copy. The agent needs to reason about join graphs,
   load order, and per-table type mappings.
2. **Be ~5–10 M rows total.** Big enough that streaming progress through
   the migration-runner is meaningful; small enough that the demo doesn't
   drag.
3. **Exercise source-specific features that don't translate cleanly to
   ClickHouse.** This is the heart of the demo. Concrete patterns to
   match against:

| Source | Feature | Why the agent has to think |
|---|---|---|
| Snowflake | `VARIANT` / `OBJECT` / `ARRAY` | Map to `JSON` or extract hot keys to typed columns? |
| Snowflake | `TIMESTAMP_TZ` / `TIMESTAMP_LTZ` | Convert to UTC at source; pick `DateTime64` precision |
| Snowflake | `STREAM` (CDC) | No CH equivalent; defer? ClickPipes? `ReplacingMergeTree`? |
| Snowflake | `DYNAMIC TABLE` | Recreate as Materialized View on `AggregatingMergeTree` |
| Snowflake | `CLUSTER BY` | Translate to ClickHouse `ORDER BY` — choose ordering |
| Postgres | `JSONB` | `JSON` column or extract hot keys |
| Postgres | Array / Enum types | `Array(T)` + decide Enum8 vs LowCardinality(String) |
| Postgres | Partial / GIN indexes | Translate to ClickHouse skip indexes? |
| Generic | High-precision `NUMBER(P, S)` | `Decimal(P, S)` not `Float` |

If your source has fewer than three such features, the demo will feel
mechanical. Add more decorations to the workload (you control the setup
script) until the agent has real decisions to make.

4. **Be reproducibly loadable.** Either the data is already in the source's
   sample catalogue (e.g. `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`) or your loader
   script seeds it from a public bucket. No partner-provided datasets —
   partners should be able to set up a clean demo in under 5 minutes.

### When TPC-H doesn't fit

If your source is fundamentally non-relational (a search engine, vector
DB, document store, time-series engine, etc.) and the TPC-H shape can't
be projected onto it sensibly, build a parallel workload that mirrors the
required properties above. Keep the row counts, fact+dimension structure,
and source-specific feature count similar to the Snowflake demo so
partners can still compare across sources.

Document your reasoning in a comment at the top of `setup_workload.sql`
(or equivalent) so future contributors know why you didn't reuse TPC-H.

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
| `sources/<source-name>/manifest.json` | All sources | Display metadata for the dashboard's Source dropdown (name, logo, source-type). Read by the runner's `GET /api/sources`. |
| `sources/<source-name>/docker/` | Local container sources | Dockerfile and/or init SQL for seeding the source database |
| `sources/<source-name>/queries/sample_olap_queries.sql` | All sources | Representative analytical queries the migration exercise will optimise |
| `sources/<source-name>/prompts/01-discover-and-design.md` | All sources | Prompt fired by the dashboard's **Discover & Design Schema** button |
| `sources/<source-name>/prompts/02-migrate-data.md` | All sources | Prompt fired by **Migrate Data** — should drive a `migrationkit` script with dispatch + ONE tail + stop |
| `sources/<source-name>/prompts/03-validate.md` | All sources | Prompt fired by **Validate** — `Validator(...).validate()` script |
| `sources/<source-name>/prompts/04-rewrite-queries.md` | All sources | Prompt fired by **Rewrite Queries** — chat-only, no script |
| `sources/<source-name>/prompts/05-benchmark.md` | All sources | Prompt fired by **Benchmark** — `Benchmarker(...).benchmark(queries=[...])` script |
| `sources/<source-name>/prompts/06-optimize.md` | All sources | Prompt fired by **Optimize** — chat reasoning, re-fire step 5 to verify |
| `sources/<source-name>/GUIDE.md` | All sources | Partner-facing migration guide. Use one of the existing sources as a template — same shape: Phase 0 (source setup), Phase 1 (launch playground), Phase 2 (six dashboard buttons), Validation, Troubleshooting. |

Optional but recommended:

| File | Purpose |
|---|---|
| `sources/<source-name>/queries/expected_ch_schema.sql` | Reference target schema the agent should arrive at — used as a comparison artifact during demos |
| `sources/<source-name>/queries/expected_ch_queries.sql` | Reference rewritten queries on ClickHouse Cloud |
| `sources/<source-name>/scripts/` | Helper scripts (TPC-H loader for sources that don't have a bundled DB; verification scripts; etc.) — used by `make tpch-load-<source>` and similar targets |

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

**Required section: "Schema Discovery — Don't Assume Anything".** Every source instructions file must open with a discovery section that mandates the `<id>-source` MCP for ALL introspection and **forbids using `migration-runner` (`run_python`) for inspection**. Without this rule, agents default to authoring Python scripts for what should be one-shot MCP calls — see [librechat/sources/snowflake-instructions.md](../librechat/sources/snowflake-instructions.md) and [librechat/sources/bigquery-instructions.md](../librechat/sources/bigquery-instructions.md) for the proven shape. The section should include a numbered checklist of one-query-per-step introspections the agent can copy verbatim (list tables with row counts/bytes, full schema per table, partitioning/clustering info, source-specific feature inventory, sample-data + cardinality checks).

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

## Customising the agent system prompt

The agent's behaviour is controlled by a set of modular instruction files. Edit them and re-run `make setup` to take effect.

| File | Purpose |
|---|---|
| [librechat/clickhouse-cloud-instructions.md](../librechat/clickhouse-cloud-instructions.md) | Base ClickHouse Cloud rules — injected into `mcpServers.clickhousectl.serverInstructions` (shared by all four agents). |
| `agent-skills/.../AGENTS.md` | ClickHouse best-practices skill (cloned from [ClickHouse/agent-skills](https://github.com/ClickHouse/agent-skills) as a submodule). Appended to the same `clickhousectl` instructions. |
| [librechat/sources/postgres-instructions.md](../librechat/sources/postgres-instructions.md) | Postgres-specific rules. |
| [librechat/sources/snowflake-instructions.md](../librechat/sources/snowflake-instructions.md) | Snowflake-specific rules. |
| [librechat/sources/bigquery-instructions.md](../librechat/sources/bigquery-instructions.md) | BigQuery-specific rules. |
| [librechat/sources/clickhouse-oss-instructions.md](../librechat/sources/clickhouse-oss-instructions.md) | ClickHouse OSS-specific rules. |

Each pre-built agent attaches exactly the MCPs for its source, so it transparently receives only the relevant `serverInstructions` — no per-agent `instructions` field is set. `make setup` rebuilds the injected blocks idempotently. **Don't edit anything below the `--- Migration Rules (auto-injected …) ---` marker in `librechat.yaml`**; the MCP-purpose blurb above the marker IS hand-editable.

**To apply changes:**

```bash
# 1. Edit the instruction file(s)
$EDITOR librechat/sources/clickhouse-oss-instructions.md

# 2. Rebuild and inject into librechat.yaml
make setup

# 3. Reload the agent
docker compose restart librechat
```

> **Existing conversations won't pick up the new prompt** — LibreChat snapshots the system prompt at conversation creation time. After changing instructions, start a new chat from the dashboard's `+` button (or click any step button to create a fresh conversation).

---

## Operational commands

Reference for all `make` targets in the project Makefile.

```bash
make setup                 # first-time setup (submodules + agent skills + .env scaffold + system prompt injection)
make up                    # start the playground (Postgres + ClickHouse OSS sources)
make up-snowflake          # also start the Snowflake source MCP + Gemini shim
make up-bigquery           # also start the BigQuery source MCP
make snowflake-setup       # set up MIGRATION_DEMO.RETAIL workload in existing Snowflake (Path A)
make snowflake-provision   # provision a fresh Snowflake demo env with Terraform (Path B)
make tpch-data             # generate TPC-H SF1 .tbl files in workloads/tpch/data/sf1/
make tpch-load-bigquery    # load TPC-H + augmentations into BigQuery
make tpch-load-postgres    # load TPC-H into Postgres
make tpch-load-clickhouse-oss # load TPC-H into the bundled ClickHouse OSS
make down                  # stop without removing data
make reset                 # destroy volumes and start fresh
make reset-agent           # delete + recreate the four pre-built agents (after AGENT_PROVIDER/AGENT_MODEL changes in .env)
make health                # check all services are healthy
make migration-status      # check progress of a running migration script (target row counts)
make logs                  # tail all service logs
make diagram               # regenerate docs/architecture.png from docs/architecture.mmd
```

### Optional: LLM tracing with Langfuse

LibreChat has native Langfuse support — add three lines to `.env` to get token usage, latency, and full conversation traces for every AI interaction.

1. Sign up at [cloud.langfuse.com](https://cloud.langfuse.com) → **Settings → API Keys**
2. Add to `.env`:
   ```bash
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_BASE_URL=https://cloud.langfuse.com
   ```
3. `docker compose restart librechat`

---

## Troubleshooting

**MCP servers not showing in LibreChat:** verify `interface.mcpServers.use: true` is set in `librechat/librechat.yaml`. Each pre-built agent attaches its MCPs automatically — if a picked agent has no MCPs in its panel, run `make reset-agent` to repair it.

**Agent not appearing in the dropdown:** `docker compose logs librechat-init` should report `✅ Created '<Agent name>'` (or `already configured — skipping`). On the very first `make up`, init runs after LibreChat reports healthy — give it ~10 seconds, then refresh the page.

**Artifacts not rendering / Sandpack crashes with `crypto.subtle` error:** artifacts require a secure context. Access the playground via `https://localhost/` through the nginx proxy — `http://` on port 3080 will not work.

**Dashboard shows "100% migrated" but `demo<x>` is empty in CH Cloud:** symptom of the now-fixed `target_database` propagation bug. If you see it on a fresh checkout, rebuild migration-runner: `docker compose build migration-runner && docker compose up -d migration-runner`. Cross-check: `system.tables WHERE database = '<CLICKHOUSE_CLOUD_DATABASE env default>'` — clean N-multiples of the source row count there is the smoking gun.

**Postgres seed takes too long:** set `DATASET_SIZE=small` in `.env`, run `make reset`. Sizes: `small` (1M, ~1 min, 4 GB RAM), `medium` (10M, 5–10 min, 8 GB RAM, default), `large` (30M, 20–30 min, 16 GB RAM).

**ClickHouse OSS MCP (`clickhouse-oss-source`) not appearing:** the MCP image uses `npx` on first start and may take 30–60 seconds to download packages. `docker compose logs clickhouse-oss-mcp -f`. Restart: `docker compose restart clickhouse-oss-mcp`.

---

## Checklist

Before opening a PR for a new source, verify:

- [ ] `sources/<source-name>/manifest.json` — present and parseable; appears in `GET /api/sources` from the runner
- [ ] `sources/<source-name>/prompts/0X-*.md` — all six prompt files exist; step 2 uses dispatch + ONE tail + stop (no `while True:` loops)
- [ ] `sources/<source-name>/queries/sample_olap_queries.sql` — all queries run against the source
- [ ] `sources/<source-name>/GUIDE.md` — all six dashboard steps run end-to-end against the new source
- [ ] `librechat/sources/<source-name>-instructions.md` — opens with a "Schema Discovery — Don't Assume Anything" section that mandates the source MCP; rules are generic (no hardcoded table/column names)
- [ ] `docker-compose.yml` — `make up` succeeds cleanly; MCP service starts and is reachable; the source is added to the `agents` array in `librechat-init` so its agent is auto-created
- [ ] `librechat/librechat.yaml` — new MCP entry under `mcpServers`; hostname added to `mcpSettings.allowedDomains`; `make setup` injects the per-source instructions without errors
- [ ] `migrationkit/sources/<source>.py` (if a new `Source` subclass is needed) — implements `count_rows`, `iter_batches`, `execute_and_count`, `close`, plus optional `unload_to_s3` / `unload_to_gcs` for staging paths; exported from `migrationkit/__init__.py`
- [ ] `make reset-agent` materialises the new agent; the dashboard's Source dropdown lists the new source
- [ ] README's migration-sources table updated
- [ ] *(Local container sources only)* `sources/<source-name>/docker/` — database seeds correctly on `docker compose up`
- [ ] *(Cloud sources only)* `.env.example` — all required credential variables documented with placeholder values; `make up-<source>` profile-gating is wired through `docker-compose.yml`
