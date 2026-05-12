# Migration Guide — Snowflake → ClickHouse Cloud

This guide walks you through a complete Snowflake → ClickHouse Cloud migration
with the AI agent in LibreChat doing the heavy lifting. The agent has live
MCP connections to your Snowflake source, your ClickHouse Cloud target, and
an in-chat Python runtime. It discovers the source schema on its own — no
hardcoded knowledge of the demo workload.

**Demo workload:** `MIGRATION_DEMO.RETAIL` — TPC-H sample tables augmented
with Snowflake-specific features (VARIANT column, TIMESTAMP_TZ column,
Clustering Key, Stream, Dynamic Table). The augmentations force the agent
into real Snowflake → ClickHouse decisions:

| Source object | ClickHouse decision |
|---|---|
| `ORDERS.ORDER_METADATA` (VARIANT) | `JSON` column, or extract hot keys to typed columns |
| `LINEITEM.DELIVERY_AT` (TIMESTAMP_TZ) | `DateTime64(3, 'UTC')` with timezone conversion |
| `LINEITEM` CLUSTER BY (...) | `ORDER BY (...)` on the target table |
| `ORDERS_CDC` (Stream) | No equivalent; recreate, replace via ClickPipes, or defer |
| `DAILY_ORDER_SUMMARY` (Dynamic Table) | ClickHouse MV on AggregatingMergeTree + backfill |

**Total time:** ~90 minutes including setup
**Prompts:** Topic-only prompts in [prompts/](prompts/) — the agent decides the steps.

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

`make snowflake-setup` installs `snowflake-connector-python`, copies the
TPC-H sample tables into `MIGRATION_DEMO.RETAIL`, and runs the
augmentations. Takes ~30 seconds on COMPUTE_WH (X-SMALL).

### Path B — Fresh demo environment via Terraform

```bash
cd sources/snowflake/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in account + admin creds
terraform init
terraform apply
```

Provisions a dedicated warehouse, role, and user, then runs the same
workload setup. `terraform output -raw env_block` prints the `.env` block
to paste back into the project root.

---

## Phase 1 — Environment (~5 min)

```bash
make up-snowflake
```

`make up-snowflake` runs `make up` plus the profile-gated `snowflake-source`
MCP container. Default `make up` skips Snowflake because the upstream MCP
crashes without valid credentials.

Open **<https://localhost>** (accept the self-signed cert). Sign in:
`admin@playground.local` / `playground`.

From the agent dropdown, pick **`Snowflake → ClickHouse Cloud`**. It ships with model, system prompt, and four MCPs (`snowflake-source`, `clickhousectl`, `clickhouse-docs`, `migration-runner`) already attached — no toggling needed.

> **Different model?** Set `AGENT_PROVIDER_SNOWFLAKE` / `AGENT_MODEL_SNOWFLAKE` in `.env` and run `make reset-agent`, or change the model in the LibreChat agent-settings panel. Google Gemini is the default-friendly choice here — the `snowflake-source-shim` service strips JSON-Schema fields the Gemini function-calling API rejects.

Test with:

> "What tables and Snowflake-specific objects are in the MIGRATION_DEMO
> database?"

The agent should list 8 tables + 1 stream + 1 dynamic table, all discovered
via SQL — no hardcoded knowledge.

---

## Phase 2 — Run the migration (~60 min)

Each step below is a single high-level prompt. Paste it into the chat,
review the agent's output before moving on, and push back when something
doesn't look right — the agent is supposed to reason, not just produce.
The same prompts are also in [prompts/](prompts/) as standalone files if
you want to copy them from there.

### Step 1 — Schema discovery

> Inventory the source Snowflake account and produce a migration
> inventory summarising what's there.

### Step 2 — Query pattern analysis

Open `sources/snowflake/queries/sample_olap_queries.sql` and paste its
contents into the chat, then send:

> Derive ORDER BY key and partition recommendations from the columns that
> appear in WHERE / JOIN / GROUP BY in these queries. Look up any
> unfamiliar Snowflake functions in the docs as you go.

### Step 3 — Schema design and implementation

> Implement the ClickHouse target schema. 

### Step 4 — Data migration

> Migrate data from snowflake to ClickHouse cloud with Python script. Check the data count at the end of migration.

> **Why the chat looks frozen during long migrations:** The `migration-runner`
> MCP runs the Python script via a synchronous `run_python` call and only
> returns stdout when the script exits. For multi-million-row tables this
> can be several minutes of apparent silence. To watch progress in real
> time from a separate terminal, run:
> ```bash
> make migration-status
> ```
> This prints whether a Python script is currently running inside the
> migration-runner, how many bytes have flowed through it, and — most
> usefully — the current row count per table on the ClickHouse Cloud
> target. Re-run as often as you like; numbers tick upward as the
> migration progresses.

### Step 5 — Query rewriting

> Rewrite the original quries
> for ClickHouse. Highlight every Snowflake-specific function or syntax
> that changed and explain why, consult the ClickHouse docs for the
> right ClickHouse function names if you're not sure.
>
> Execute each rewritten query to verify correctness on the migrated data.

### Step 6 — Materialized View optimisation

> Propose Materialized View optimisations for the heaviest aggregation
> queries.

### Step 7 — Validation and post-migration report

> Cross-check the migrated schema and data.
> Generate the post-migration HTML report.

### Step 8 - Performance benchmark
> Benchmark queries to show the performance difference. Present results in table format.
---

## Validation

Compare the agent's final state against:

- **Schema:** [queries/expected_ch_schema.sql](queries/expected_ch_schema.sql)
- **Queries:** [queries/expected_ch_queries.sql](queries/expected_ch_queries.sql)

Bit-for-bit identity isn't expected — what matters is that the agent made
defensible choices: `Decimal` (not `Float`) for money, `DateTime64(3, 'UTC')`
for the augmented timezone-aware column, `JSON` (not `String`) for the
VARIANT column, a Materialized View on AggregatingMergeTree replacing the
Dynamic Table, and a clear decision on what to do with the Stream.

---

## Troubleshooting

**Snowflake MCP container restarts / shows unhealthy:**
The upstream `snowflake-labs-mcp` opens a Snowflake connection at startup
and exits if credentials are invalid. Check `docker compose logs
snowflake-source` and fix `.env`, then
`docker compose --profile snowflake up -d snowflake-source`.

**Migration runner can't reach Snowflake from inside the container:**
The `migration-runner` container picks up `.env` via `env_file`. If you
edited `.env` after `make up`, restart the runner:
`docker compose restart migration-runner`.

**Step 4 looks frozen — chat is silent for minutes:**
`run_python` is synchronous and only returns stdout when the script exits.
Run `make migration-status` from a separate terminal to confirm the script
is still running and to see live row counts populating on the ClickHouse
Cloud side.

**Agent doesn't know about a Snowflake feature it just discovered:**
Prompt it to fetch the docs — e.g. *"Look up
`https://docs.snowflake.com/en/user-guide/dynamic-tables-about.md` and
explain Dynamic Tables before deciding how to migrate this one."* Once
the agent has the docs in context it should reason correctly.
