# Migration Guide — Snowflake → ClickHouse Cloud

This guide walks you through a complete Snowflake → ClickHouse Cloud
migration using the **MigrationRoom** dashboard. The dashboard
orchestrates the work: you pick a source, click six step buttons in
order, and watch the AI agent do each step live. The agent has MCP
connections to your Snowflake source, your ClickHouse Cloud target, an
in-chat Python runtime, and the `migrationkit` Python library that
handles data movement.

**Demo workload:** `MIGRATION_DEMO.RETAIL` — TPC-H sample tables
augmented with Snowflake-specific features (VARIANT column,
TIMESTAMP_TZ column, Clustering Key, Stream, Dynamic Table). The
augmentations force the agent into real Snowflake → ClickHouse
decisions:

| Source object | ClickHouse decision |
|---|---|
| `ORDERS.ORDER_METADATA` (VARIANT) | `JSON` column, or extract hot keys to typed columns |
| `LINEITEM.DELIVERY_AT` (TIMESTAMP_TZ) | `DateTime64(3, 'UTC')` with timezone conversion |
| `LINEITEM` CLUSTER BY (...) | `ORDER BY (...)` on the target table |
| `ORDERS_CDC` (Stream) | No equivalent; recreate, replace via ClickPipes, or defer |
| `DAILY_ORDER_SUMMARY` (Dynamic Table) | ClickHouse MV on AggregatingMergeTree + backfill |

**Total time:** ~60 minutes including setup.
**Workflow:** the dashboard's six step buttons drive the migration. The
prompt files in [prompts/](prompts/) are what each button fires — you
don't need to paste them by hand.

---

## Phase 0 — Snowflake Setup (~5 min)

Pick one path. Both end with the same `MIGRATION_DEMO.RETAIL` workload
sitting in your Snowflake account.

### Path A — Existing Snowflake account

```bash
# 1. Activate a venv so the script's pip installs don't touch system Python.
python3 -m venv .venv && source .venv/bin/activate

# 2. Set SNOWFLAKE_ACCOUNT/USER/PASSWORD in .env. Other vars (warehouse,
#    role) have sensible defaults but can be overridden.

# 3. Export .env into the shell and run the setup.
set -a; source .env; set +a
make snowflake-setup
```

`make snowflake-setup` installs `snowflake-connector-python`, copies
the TPC-H sample tables into `MIGRATION_DEMO.RETAIL`, and runs the
augmentations. Takes ~30 seconds on COMPUTE_WH (X-SMALL).

### Path B — Fresh demo environment via Terraform

```bash
cd sources/snowflake/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in account + admin creds
terraform init
terraform apply
```

Provisions a dedicated warehouse, role, and user, then runs the same
workload setup. `terraform output -raw env_block` prints the `.env`
block to paste back into the project root.

---

## Phase 1 — Launch the playground (~5 min)

```bash
make up-snowflake
```

`make up-snowflake` runs `make up` plus the profile-gated
`snowflake-source` MCP container. Default `make up` skips Snowflake
because the upstream MCP crashes without valid credentials.

Open **<https://localhost/dashboard/>** (accept the self-signed cert)
and sign in (`admin@playground.local` / `playground`). You'll land on
the **MigrationRoom** dashboard with the chat panel on the right.

In the **SETUP** card at the top:

- **Source**: pick `Snowflake`. The chat panel auto-switches to the
  `Snowflake → ClickHouse Cloud` agent.
- **Source database**: pick `MIGRATION_DEMO` (or whatever DB you
  populated in Phase 0).
- **Queries**: open **Edit · N OLAP** and confirm the OLAP queries are
  loaded (steps 4 and 5 use them for rewrite + benchmark).

> **Different model?** Set `AGENT_PROVIDER_SNOWFLAKE` /
> `AGENT_MODEL_SNOWFLAKE` in `.env` and run `make reset-agent`, or
> change the model in the LibreChat agent-settings panel. Google
> Gemini is the default-friendly choice for Snowflake — the
> `snowflake-source-shim` service strips JSON-Schema fields the Gemini
> function-calling API rejects.

---

## Phase 2 — Run the migration (~45 min)

The dashboard has **six step buttons** at the top of the **STEPS**
panel. Click each in order. Every click fires a prompt to the agent
and tracks progress live; you don't paste prompts by hand. All six
buttons are clickable at any time, so you can re-fire a step (e.g. to
re-run validation after fixing the schema).

### Step 1 — Discover & Design Schema

Agent introspects the source via `snowflake-source` MCP (`SHOW
DATABASES`, `SHOW TABLES`, `GET_DDL` per object), reads your OLAP
queries to drive `ORDER BY` choices, proposes the ClickHouse target
schema, and runs the DDL via `clickhousectl`.

**Watch in chat** for the agent's decisions: `Decimal(P,S)` for money,
`DateTime64(3, 'UTC')` for the augmented timezone column, `JSON` (not
`String`) for `ORDER_METADATA`, an explicit call on what to do with
the `ORDERS_CDC` Stream and the `DAILY_ORDER_SUMMARY` Dynamic Table.
**Confirm the target database name** when the agent asks (default
suggestion `migration_demo`).

### Step 2 — Migrate Data

Agent writes a short Python script using the `migrationkit` library,
dispatches it as a background job via `migration-runner`, issues
ONE `tail_python_job` to confirm `status=running`, then stops. The
dashboard's **Migration** tab streams live progress: rows/sec, ETA,
per-table progress bars, milestone events.

Snowflake → ClickHouse Cloud large tables can also take the S3-stage
path (`COPY INTO @stage` → `INSERT FROM s3()`); the agent picks the
right path per table based on row count and whether `STAGING_S3_*`
env vars are set.

You don't need to do anything during this step except watch the bars
fill. The chat will be quiet — that's intentional.

### Step 3 — Validate

Agent runs `Validator(...).validate()` — row count parity per table,
source vs target. Results land on the dashboard's **Validation** tab
(one row per table: source rows / target rows / matched). If anything
mismatches the agent **stops and reports** in chat — fix the schema
and re-fire step 2, don't ask the agent to patch the target by hand.

### Step 4 — Rewrite Queries

Agent translates each OLAP query from Snowflake dialect to ClickHouse
SQL **in chat**. No script — this is a reasoning step. Walk through
each rewrite, push back on unfamiliar function substitutions
(`DATEADD` → `addDays`, `OBJECT_CONSTRUCT` → tuple literal, etc.).

### Step 5 — Benchmark

Agent runs `Benchmarker(...).benchmark(queries=[...])` — each query on
source and target, server-side timing on both. Results land on the
**Benchmark** tab as `source_ms / target_ms / speedup` per query.

Snowflake timing comes from
`SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY.EXECUTION_TIME`; ClickHouse
timing from the `X-ClickHouse-Summary` HTTP header. Wall-clock is also
recorded for diagnostic display when the gap is meaningful.

### Step 6 — Optimize

Agent proposes ClickHouse-Cloud-specific optimizations for the
slowest queries: Materialized Views on `AggregatingMergeTree`,
Projections, codec adjustments. Iterate in chat — once you apply an
optimization, re-fire step 5 to confirm the speedup.

---

## Validation

Compare the agent's final state against:

- **Schema:** [queries/expected_ch_schema.sql](queries/expected_ch_schema.sql)
- **Queries:** [queries/expected_ch_queries.sql](queries/expected_ch_queries.sql)
- **Checklist:** [../../docs/migration-checklist.md](../../docs/migration-checklist.md)

Bit-for-bit identity isn't expected — what matters is that the agent
made defensible choices: `Decimal` (not `Float`) for money,
`DateTime64(3, 'UTC')` for the augmented timezone-aware column, `JSON`
(not `String`) for the VARIANT column, a Materialized View on
`AggregatingMergeTree` replacing the Dynamic Table, and a clear
decision on what to do with the Stream.

For a long-form narrative walkthrough of the canonical Snowflake
demo, see [demo.md](demo.md).

---

## Troubleshooting

**Snowflake MCP container restarts / shows unhealthy:**
The upstream `snowflake-labs-mcp` opens a Snowflake connection at
startup and exits if credentials are invalid. Check `docker compose
logs snowflake-source` and fix `.env`, then `docker compose --profile
snowflake up -d snowflake-source`.

**Migration runner can't reach Snowflake from inside the container:**
The `migration-runner` container picks up `.env` via `env_file`. If
you edited `.env` after `make up`, restart the runner: `docker compose
restart migration-runner`.

**Step 2 looks quiet in chat:**
Expected — the agent dispatches the migration as a background job and
stops. Watch the dashboard's **Migration** tab for live row counts and
throughput. For an out-of-band view, `make migration-status` from a
separate terminal prints whether a script is running plus current row
counts on the ClickHouse Cloud target.

**Step 3 reports a mismatch:**
The agent will stop and surface the mismatched table(s) in chat.
Don't ask it to patch the target with manual `INSERT … SELECT … FROM
…` — that bypasses the Migrator and hides the underlying bug. Fix
the schema (most common cause: case-sensitivity, or a column the
agent left out) and re-fire step 2.

**Agent doesn't know about a Snowflake feature it just discovered:**
Prompt it to fetch the docs — e.g. *"Look up
`https://docs.snowflake.com/en/user-guide/dynamic-tables-about` and
explain Dynamic Tables before deciding how to migrate this one."*
Once the agent has the docs in context it should reason correctly.
