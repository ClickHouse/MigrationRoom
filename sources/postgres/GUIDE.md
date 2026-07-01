# Migration Guide — PostgreSQL → ClickHouse Cloud

This guide walks you through a complete Postgres → ClickHouse Cloud
migration using the **MigrationRoom** dashboard. The dashboard
orchestrates the work: you pick a source, click six step buttons in
order, and watch the AI agent do each step live. The agent has MCP
connections to your Postgres source, your ClickHouse Cloud target, an
in-chat Python runtime, and the `migrationkit` Python library that
handles data movement.

**Demo workloads (pick one):**

- **`ecommerce`** — bundled with the playground, ~10M rows across
  users, products, sessions, events, orders, order_items,
  ad_impressions, inventory_snapshots. Exercises Postgres-specific
  features (JSONB, ENUM, ARRAY, TIMESTAMPTZ, BIGSERIAL, GIN indexes).
  Seeded automatically by `make up`.
- **`tpch`** — same TPC-H SF1 workload used by the Snowflake /
  BigQuery / ClickHouse OSS demos, with augmentations that make a
  Postgres → ClickHouse migration interesting. Load with `make
  tpch-load-postgres` after `make up`.

**Total time:** ~45 minutes including setup.
**Workflow:** the dashboard's six step buttons drive the migration. The
prompt files in [prompts/](prompts/) are what each button fires — you
don't need to paste them by hand.

---

## Phase 0 — Postgres Setup (~5 min)

Postgres runs as a container inside the playground — no external
account required.

```bash
git clone https://github.com/sishuoyang/MigrationRoom
cd MigrationRoom
make setup
```

Edit `.env` — add your LLM API key and ClickHouse Cloud credentials:

```bash
ANTHROPIC_API_KEY=sk-ant-...        # or OPENAI_API_KEY=sk-...
CLICKHOUSE_CLOUD_HOST=<your-service>.clickhouse.cloud
CLICKHOUSE_CLOUD_USER=default
CLICKHOUSE_CLOUD_PASSWORD=<your-password>
```

---

## Phase 1 — Launch the playground (~5 min)

```bash
make up
# First run: Postgres seeds ~10M rows for `ecommerce` — allow 5–10 minutes.
docker compose logs postgres -f   # watch seed progress
```

If you want the TPC-H workload too, run it after `make up` is healthy:

```bash
make tpch-load-postgres
```

Open **<https://localhost/dashboard/>** (accept the self-signed cert)
and sign in (`admin@playground.local` / `playground`). You'll land on
the **MigrationRoom** dashboard with the chat panel on the right.

In the **SETUP** card at the top:

- **Source**: pick `Postgres`. The chat panel auto-switches to the
  `Postgres → ClickHouse Cloud` agent.
- **Source database**: pick `ecommerce` (the bundled workload) or
  `tpch` (if you loaded it).
- **Queries**: open **Edit · N OLAP** and confirm the OLAP queries are
  loaded (steps 4 and 5 use them for rewrite + benchmark).

> **Different model?** Set `AGENT_PROVIDER_POSTGRES` /
> `AGENT_MODEL_POSTGRES` in `.env` and run `make reset-agent`, or
> change the model in the LibreChat agent-settings panel.

---

## Phase 2 — Run the migration (~35 min)

The dashboard has **six step buttons** at the top of the **STEPS**
panel. Click each in order. Every click fires a prompt to the agent
and tracks progress live; you don't paste prompts by hand. All six
buttons are clickable at any time, so you can re-fire a step (e.g. to
re-run validation after fixing the schema).

### Step 1 — Discover & Design Schema

Agent introspects the source via `postgres-source` MCP (`execute_sql`
against `information_schema`, `pg_class`, `pg_indexes`, `pg_type`),
reads your OLAP queries to drive `ORDER BY` choices, proposes the
ClickHouse target schema, and runs the DDL via `clickhousectl`.

**Watch in chat** for the agent's Postgres-specific decisions: JSONB
→ `JSON` (or `String` for parse-on-read), ARRAY → `Array(T)`, ENUM →
`LowCardinality(String)` (or `Enum8` for truly closed value sets),
TIMESTAMPTZ → `DateTime64(_, 'UTC')`, SERIAL/IDENTITY → smallest
`UInt*` that fits, nullable columns → `Nullable(T)` *or* non-Nullable
with an explicit `DEFAULT` plus a step-2 transform that maps `None`
to the default. **Confirm the target database name** when the agent
asks (default suggestion `migration_demo`).

### Step 2 — Migrate Data

Agent writes a short Python script using the `migrationkit` library,
dispatches it as a background job via `migration-runner`, issues
ONE `tail_python_job` to confirm `status=running`, then stops. The
dashboard's **Migration** tab streams live progress: rows/sec, ETA,
per-table progress bars, milestone events.

The agent picks per-table batch sizes (small dims at 100K rows, big
facts often at 50K to keep ClickHouse-Cloud HTTP payloads
predictable) and adds `transform=` lambdas for any nullable column
that lands in a non-Nullable target column.

You don't need to do anything during this step except watch the bars
fill. The chat will be quiet — that's intentional.

### Step 3 — Validate

Agent runs `Validator(...).validate()` — row count parity per table,
source vs target. Results land on the dashboard's **Validation** tab
(one row per table: source rows / target rows / matched). If anything
mismatches the agent **stops and reports** in chat — fix the schema
and re-fire step 2, don't ask the agent to patch the target by hand.

### Step 4 — Rewrite Queries

Agent translates each OLAP query from Postgres SQL to ClickHouse SQL
**in chat**. No script — this is a reasoning step. Walk through each
rewrite, push back on unfamiliar function substitutions
(`date_trunc('month', x)` → `toStartOfMonth(x)`,
`generate_series(...)` → array-of-numbers idioms, `jsonb_extract_path`
→ `JSONExtract*`, window-function variants, etc.).

### Step 5 — Benchmark

Agent runs `Benchmarker(...).benchmark(queries=[...])` — each query
on source and target, server-side timing on both. Results land on the
**Benchmark** tab as `source_ms / target_ms / speedup` per query.

Postgres timing comes from `EXPLAIN (ANALYZE, FORMAT JSON,
BUFFERS).Execution Time` — note that **benchmark SQL must be
read-only**, since `EXPLAIN ANALYZE` executes any DML it wraps.
ClickHouse timing comes from the `X-ClickHouse-Summary` HTTP header.

### Step 6 — Optimize

Agent proposes ClickHouse-Cloud-specific optimizations for the
slowest queries: Materialized Views on `AggregatingMergeTree`,
Projections, codec adjustments. Iterate in chat — once you apply an
optimization, re-fire step 5 to confirm the speedup.

---

## Validation

When complete, compare results against the reference solutions:

- **Schema:** [queries/expected_ch_schema.sql](queries/expected_ch_schema.sql)
- **Queries:** [queries/expected_ch_queries.sql](queries/expected_ch_queries.sql)
- **Checklist:** [../../docs/migration-checklist.md](../../docs/migration-checklist.md)

Bit-for-bit identity isn't expected — what matters is that the agent
made defensible Postgres-specific choices: `Decimal` (not `Float`)
for money, `JSON` (not `String`) for JSONB columns,
`LowCardinality(String)` (not `Enum8`) for status-like columns where
the value set isn't truly closed, `DateTime64(_, 'UTC')` for
TIMESTAMPTZ, and `Nullable(T)` *or* `T DEFAULT <literal>` for every
nullable source column.

---

## Troubleshooting

**Postgres MCP container restarts / shows unhealthy:**
The `postgres-mcp` container connects to the `postgres` service at
startup. If `postgres` is still seeding (10M rows on first `make up`)
the MCP can race ahead and fail its healthcheck. Re-check after
seeding completes: `docker compose logs postgres -f` → `make up`.

**Migration runner can't reach Postgres from inside the container:**
The `migration-runner` container picks up `.env` via `env_file`. If
you edited `.env` after `make up`, restart the runner: `docker
compose restart migration-runner`.

**Step 2 looks quiet in chat:**
Expected — the agent dispatches the migration as a background job and
stops. Watch the dashboard's **Migration** tab for live row counts
and throughput.

**Step 3 reports a mismatch:**
The agent will stop and surface the mismatched table(s) in chat.
Don't ask it to patch the target with manual `INSERT … SELECT … FROM
…` — that bypasses the Migrator and hides the underlying bug. Fix
the schema (most common cause: a nullable column without
`Nullable(T)` or a `DEFAULT`, or a JSONB column with the wrong target
type) and re-fire step 2.

**`Unknown element '...' for enum` mid-batch:**
Postgres ENUM types can have values added via `ALTER TYPE` that the
schema definition doesn't show. Before designing the target column,
run `SELECT DISTINCT <enum_col> FROM <table>` on `postgres-source` —
or just declare the target as `LowCardinality(String)` which is the
safe default for migrated columns.

**Step 5 errors with "function does not exist" on Postgres side:**
Some of the OLAP queries the agent benchmarks use Postgres features
the source database doesn't have installed. Either install the
missing extension on the source, or remove that query from the
**Edit · N OLAP** panel before clicking step 5.
