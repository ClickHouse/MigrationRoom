# Migration Guide — ClickHouse OSS → ClickHouse Cloud

This guide walks you through a complete ClickHouse OSS → ClickHouse
Cloud migration using the **MigrationRoom** dashboard. The dashboard
orchestrates the work: you pick a source, click six step buttons in
order, and watch the AI agent do each step live. The agent has MCP
connections to your ClickHouse OSS source, your ClickHouse Cloud
target, an in-chat Python runtime, and the `migrationkit` Python
library that handles data movement.

**Demo workloads (pick one):**

- **`analytics`** — bundled with the playground, ~12.2M rows. Web
  analytics platform (projects, sessions, pageviews, conversions, plus
  an `AggregatingMergeTree` + `MaterializedView` for daily stats).
  Exercises ClickHouse-OSS-specific features that map to Cloud:
  `AggregatingMergeTree` recreate-and-backfill, projections, skip
  indexes. Seeded automatically by `make up`.
- **`tpch`** — same TPC-H SF1 workload used by the other demos. Load
  with `make tpch-load-clickhouse-oss` after `make up`.

**Total time:** ~35 minutes including setup.
**Workflow:** the dashboard's six step buttons drive the migration. The
prompt files in [prompts/](prompts/) are what each button fires — you
don't need to paste them by hand.

---

## Phase 0 — ClickHouse OSS Setup (~5 min)

ClickHouse OSS runs as a container inside the playground — no external
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
# First run: ClickHouse OSS seeds ~12.2M rows for `analytics` — allow 3–5 min.
docker compose logs clickhouse-oss -f   # watch seed progress
```

If you want the TPC-H workload too, run it after `make up` is healthy:

```bash
make tpch-load-clickhouse-oss
```

Open **<https://localhost/dashboard/>** (accept the self-signed cert)
and sign in (`admin@playground.local` / `playground`). You'll land on
the **MigrationRoom** dashboard with the chat panel on the right.

In the **SETUP** card at the top:

- **Source**: pick `ClickHouse OSS`. The chat panel auto-switches to
  the `ClickHouse OSS → ClickHouse Cloud` agent.
- **Source database**: pick `analytics` (bundled) or `tpch` (if you
  loaded it).
- **Queries**: open **Edit · N OLAP** and confirm the OLAP queries are
  loaded (steps 4 and 5 use them for rewrite + benchmark).

> **Different model?** Set `AGENT_PROVIDER_CLICKHOUSE_OSS` /
> `AGENT_MODEL_CLICKHOUSE_OSS` in `.env` and run `make reset-agent`,
> or change the model in the LibreChat agent-settings panel.

---

## Phase 2 — Run the migration (~25 min)

The dashboard has **six step buttons** at the top of the **STEPS**
panel. Click each in order. Every click fires a prompt to the agent
and tracks progress live; you don't paste prompts by hand. All six
buttons are clickable at any time, so you can re-fire a step (e.g. to
re-run validation after fixing the schema).

### Step 1 — Discover & Design Schema

Agent introspects the source via `clickhouse-oss-source` MCP
(`system.tables`, `SHOW CREATE TABLE`, `DESCRIBE TABLE`,
`system.projection_parts`, `system.data_skipping_indices`), reads
your OLAP queries to drive `ORDER BY` choices, proposes the
ClickHouse Cloud target schema, and runs the DDL via `clickhousectl`.

**Watch in chat** for the OSS-to-Cloud-specific decisions: preserve
`PARTITION BY` and `ORDER BY` keys from the source (changing them
after data is loaded means a full rewrite); flag
`AggregatingMergeTree` tables for the recreate-and-backfill pattern
(don't copy binary state columns straight across); decide whether to
preserve each projection / skip index on the target. **Confirm the
target database name** when the agent asks (default suggestion
`migration_demo`).

### Step 2 — Migrate Data

Agent writes a short Python script using the `migrationkit` library,
dispatches it as a background job via `migration-runner`, issues
ONE `tail_python_job` to confirm `status=running`, then stops. The
dashboard's **Migration** tab streams live progress: rows/sec, ETA,
per-table progress bars, milestone events.

For OSS sources `migrationkit` also supports the S3-stage path
(`INSERT INTO FUNCTION s3(...)` on the source → `INSERT FROM s3()`
on the target); the agent picks the right path per table based on
row count and whether `STAGING_S3_*` env vars are set.

`AggregatingMergeTree` tables are NOT migrated row-by-row. The agent
recreates the underlying raw table and the `MaterializedView` on the
target, then runs a backfill `INSERT INTO <agg_table> SELECT … FROM
<raw_table>` to reconstruct the aggregate state.

You don't need to do anything during this step except watch the bars
fill. The chat will be quiet — that's intentional.

### Step 3 — Validate

Agent runs `Validator(...).validate()` — row count parity per table,
source vs target. Results land on the dashboard's **Validation** tab
(one row per table: source rows / target rows / matched). If anything
mismatches the agent **stops and reports** in chat — fix the schema
and re-fire step 2, don't ask the agent to patch the target by hand.

### Step 4 — Rewrite Queries

Most ClickHouse-OSS queries run on ClickHouse Cloud unchanged. Step 4
is for the few that do need attention: OSS-only functions, settings
that differ on Cloud, or queries that hit the OSS-specific
`MaterializedView` plumbing. The agent walks through each query and
calls out the differences in chat.

### Step 5 — Benchmark

Agent runs `Benchmarker(...).benchmark(queries=[...])` — each query
on source and target, server-side timing on both (`elapsed_ns` from
`X-ClickHouse-Summary` on both sides). Results land on the
**Benchmark** tab as `source_ms / target_ms / speedup` per query.

This is the most informative step for an OSS → Cloud migration —
shared-storage Cloud usually wins on parallel scan, but OSS sometimes
wins on cache-warm point lookups; the per-query breakdown tells you
where to focus.

### Step 6 — Optimize

Agent proposes Cloud-specific optimizations for the slowest queries:
Projections (Cloud-friendly), Materialized Views on
`AggregatingMergeTree`, codec tuning, `ORDER BY` adjustments where
the source key turned out wrong for the workload. Iterate in chat —
once you apply an optimization, re-fire step 5 to confirm the
speedup.

---

## Validation

When complete, compare results against the reference solutions:

- **Schema:** [queries/expected_ch_schema.sql](queries/expected_ch_schema.sql)
- **Queries:** [queries/expected_ch_queries.sql](queries/expected_ch_queries.sql)
- **Checklist:** [../../docs/migration-checklist.md](../../docs/migration-checklist.md)

Bit-for-bit identity isn't expected — what matters is that the agent
preserved the source `PARTITION BY` / `ORDER BY` keys (unless there
was a clear reason to change them), handled `AggregatingMergeTree`
via recreate-and-backfill (not raw row copy), recreated the
`MaterializedView` on the target, and decided on each projection /
skip index based on the workload.

---

## Troubleshooting

**ClickHouse OSS MCP container restarts / shows unhealthy:**
`clickhouse-oss-mcp` connects to the `clickhouse-oss` service at
startup. If `clickhouse-oss` is still seeding (12.2M rows on first
`make up`) the MCP can race ahead and fail its healthcheck. Re-check
after seeding completes.

**Migration runner can't reach OSS from inside the container:**
The `migration-runner` container picks up `.env` via `env_file`. If
you edited `.env` after `make up`, restart the runner: `docker
compose restart migration-runner`.

**Step 2 looks quiet in chat:**
Expected — the agent dispatches the migration as a background job and
stops. Watch the dashboard's **Migration** tab for live row counts
and throughput.

**Step 3 reports a mismatch on `daily_stats` (or other agg table):**
`AggregatingMergeTree` row counts depend on merges. After backfill,
the partial parts may not have merged yet — the visible row count
counts unmerged parts. Either wait for background merges (`OPTIMIZE
TABLE … FINAL` to force) and re-fire step 3, or accept the discrepancy
if the `count()` on raw data matches.

**Step 4 says "no rewrites needed":**
Common for OSS → Cloud — most queries port unchanged. Skip to step 5.
