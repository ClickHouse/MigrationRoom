## Snowflake Source — Migration Instructions

These rules apply when the data source is Snowflake. **The schema is unknown
ahead of time** — every connected account has different tables, columns, and
Snowflake-specific objects. Discover the schema dynamically before reasoning
about migration.

---

## Identifier Case — Always Uppercase

Snowflake folds unquoted identifiers to uppercase. The MCP returns metadata
with uppercase names. Reference all Snowflake objects in **UPPERCASE** to
avoid silent lookup failures:

```sql
-- WRONG
SELECT count(*) FROM my_db.my_schema.my_table;

-- CORRECT
SELECT count(*) FROM MY_DB.MY_SCHEMA.MY_TABLE;
```

In the ClickHouse target schema, use lowercase by convention. Don't carry
Snowflake's uppercase into ClickHouse.

---

## Schema Discovery — Don't Assume Anything

Never assume column names, table names, types, or that any particular
Snowflake feature is or is not in use. Discover the actual schema at runtime
via the `snowflake-source` MCP.

**Use `snowflake-source` (not `migration-runner`) for all schema discovery
and read-only inspection of Snowflake.** The `migration-runner` MCP is
**only** for data movement (Path 1) once the schema is understood. Reasons:

- `snowflake-source` is account-scoped — `SHOW DATABASES` returns every
  database the user can see, not just one default.
- `migration-runner` inherits env vars from the playground's `.env` via
  `env_file:`. If a stale `SNOWFLAKE_DATABASE` / `SNOWFLAKE_SCHEMA` is set
  there from a previous workload, a `snowflake-connector-python`
  connection through `migration-runner` will silently scope to that
  database/schema and your inventory will miss everything else.
- When you do need Python on `migration-runner`, **never** pass
  `database=` or `schema=` to `snowflake.connector.connect(...)`. Discover
  with `snowflake-source` first; only then `USE DATABASE`/`USE SCHEMA`
  inside the Python script with names you explicitly chose.

**The discovery checklist for every Snowflake migration:**

1. **List databases and schemas:**
   ```sql
   SHOW DATABASES;
   SHOW SCHEMAS IN DATABASE <db>;
   ```

2. **List every kind of object** — Snowflake separates them, and a `SHOW
   TABLES` alone misses streams, dynamic tables, tasks, and materialized
   views:
   ```sql
   SHOW TABLES            IN SCHEMA <db>.<schema>;
   SHOW VIEWS             IN SCHEMA <db>.<schema>;
   SHOW MATERIALIZED VIEWS IN SCHEMA <db>.<schema>;
   SHOW STREAMS           IN SCHEMA <db>.<schema>;
   SHOW DYNAMIC TABLES    IN SCHEMA <db>.<schema>;
   SHOW TASKS             IN SCHEMA <db>.<schema>;
   SHOW ICEBERG TABLES    IN SCHEMA <db>.<schema>;
   ```

3. **Get full DDL** for each object — use `GET_DDL` to see the canonical
   CREATE statement including all column types, defaults, clustering keys,
   and (for Dynamic Tables) the underlying query:
   ```sql
   SELECT GET_DDL('TABLE',         '<db>.<schema>.<object>');
   SELECT GET_DDL('VIEW',          '<db>.<schema>.<object>');
   SELECT GET_DDL('STREAM',        '<db>.<schema>.<object>');
   SELECT GET_DDL('DYNAMIC_TABLE', '<db>.<schema>.<object>');
   ```

4. **Sample data and check nullability + cardinality** for every column
   before designing the target. This is what tells you whether a column is
   `Nullable(T)`, whether a string is a `LowCardinality(String)` candidate,
   and whether a NUMBER column needs full Decimal precision:
   ```sql
   SELECT * FROM <db>.<schema>.<table> LIMIT 5;
   SELECT COUNT(*), COUNT(<col>) FROM <db>.<schema>.<table>;
   SELECT COUNT(DISTINCT <col>) FROM <db>.<schema>.<table>;
   ```

5. **Inspect the query patterns** the partner shares. ORDER BY key
   selection on the ClickHouse side should come from the columns that
   appear in WHERE / JOIN / GROUP BY of the actual queries — not from the
   PK or clustering key on the Snowflake side.

Produce a migration inventory before generating any target schema.

---

## Looking Up Snowflake Documentation

When the agent encounters a Snowflake feature it doesn't fully understand
(Streams semantics, Dynamic Table refresh modes, VARIANT vs OBJECT vs
ARRAY, `CONVERT_TIMEZONE` semantics, Time Travel, Iceberg, Hybrid Tables,
Snowpark, etc.), fetch the official docs **as markdown** via WebFetch by
appending `.md` to the URL:

```
WebFetch https://docs.snowflake.com/en/user-guide/streams-intro.md
WebFetch https://docs.snowflake.com/en/sql-reference/sql/create-dynamic-table.md
WebFetch https://docs.snowflake.com/en/sql-reference/data-types-semistructured.md
WebFetch https://docs.snowflake.com/en/sql-reference/functions/convert_timezone.md
```

This is Snowflake's official LLM-friendly endpoint — every page on
`docs.snowflake.com/en/<path>` is also served as `<path>.md`. Use it freely.

Cross-reference findings against the ClickHouse side via the
`clickhouse-docs` MCP (`search_click_house_documentation(...)`) to identify
the right ClickHouse equivalent — or to confirm there is none.

---

## Looking Up ClickHouse Best Practices

Always consult `clickhouse-docs` when designing the target schema. The areas
that benefit most from a docs lookup before committing to DDL:

- ORDER BY key selection
- PARTITION BY granularity
- Decimal vs Float precision tradeoffs
- LowCardinality threshold
- Nullable vs sentinel-value choice
- Materialized View patterns (AggregatingMergeTree, SimpleAggregateFunction)
- Skip indexes (minmax, set, Bloom)
- Iceberg / s3() table functions

The `clickhousectl` MCP also has the full best-practices reference embedded
in its server instructions — those rules are authoritative for the target.

---

## Two Migration Paths — Always Ask First

Before generating any migration code, present both paths and let the partner
choose.

### Path 1 — Python via the `migration-runner` MCP

`snowflake-connector-python` reads from the source; `clickhouse-connect`
writes to the target. The agent calls `run_python` on `migration-runner`
and streams output back into the chat. Use this when:
- Type coercion is non-trivial (VARIANT, OBJECT, NUMBER precision)
- You want explicit batching by date range or ID range
- The partner wants a connector-pattern walkthrough

**Rule:** never paste Python into the chat for the partner to run locally.
Always invoke `run_python` and stream the output.

The migration-runner container sees these env vars: `SNOWFLAKE_ACCOUNT`,
`SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_WAREHOUSE`,
`SNOWFLAKE_ROLE`, `CLICKHOUSE_CLOUD_HOST`, `CLICKHOUSE_CLOUD_USER`,
`CLICKHOUSE_CLOUD_PASSWORD`, `CLICKHOUSE_CLOUD_DATABASE`.

Pre-installed libraries: `snowflake-connector-python`, `clickhouse-connect`,
`psycopg2`, `pyarrow`. Don't `pip install` anything inside `run_python`;
the runner user has no install permission.

**Two tools — pick the right one.**

| Tool | When to use |
|---|---|
| `run_python(code, timeout_seconds=3600)` | Synchronous, one tool call. Use ONLY for short scripts (<60 s) — schema checks, validation queries, small inserts. Output appears only when the script exits, so for anything longer the chat stays blank for the whole run. |
| `run_python_background(code, timeout_seconds=3600)` + `tail_python_job(job_id, stdout_offset, stderr_offset, max_wait_seconds=60)` | **Mandatory for actual data migrations.** Launches in the background, returns a `job_id` immediately. Then call `tail_python_job` in a loop, passing back the offsets each time, until `status == "done"`. Every tail call's tool-result is visible in the chat, giving the partner a chunk of streaming progress every ~60 s. |

**Background+tail pattern — use this whenever the script will run >60s:**

```python
# 1. Launch the migration in the background
launch = run_python_background(code=migration_script, timeout_seconds=3600)
job_id = launch["job_id"]

# 2. Tail-poll loop. Pass returned offsets back on each call.
stdout_off, stderr_off = 0, 0
while True:
    r = tail_python_job(
        job_id=job_id,
        stdout_offset=stdout_off,
        stderr_offset=stderr_off,
        max_wait_seconds=60,
    )
    stdout_off = r["stdout_offset"]
    stderr_off = r["stderr_offset"]
    if r["status"] == "done":
        break
```

**Timeout sizing.** Both `run_python` and `run_python_background` default to
3600 s (1 h). For multi-million-row migrations, pass an explicit
`timeout_seconds` — roughly 1 second per ~5K rows is a safe starting
estimate when going Snowflake → ClickHouse Cloud over the public internet.

**Resume on timeout.** If a script times out partway through, write a
**resume-aware** retry: check `count()` per target table in ClickHouse,
skip tables already at the expected row count, `TRUNCATE` partially-loaded
tables, and only re-run the unfinished ones. Never blindly re-run the
whole script — the dimensions are already done.

### Path 2 — Direct `s3()` import via `clickhousectl`

ClickHouse Cloud reads parquet/CSV/JSON from S3/GCS in parallel using the
`s3()` table function. Production-canonical for large workloads. Viable
only when:
- The source data is already in object storage, OR
- The partner can `COPY INTO @stage` from Snowflake to S3/GCS first

If neither, use Path 1.

---

## Snowflake → ClickHouse Type Mapping

Quick Snowflake-type → ClickHouse-type lookup. The ClickHouse best-practices
rules (already in this agent's prompt) cover the type-choice nuances —
LowCardinality threshold, Nullable avoidance, Enum vs LowCardinality, Decimal
vs Float, DateTime vs DateTime64 width, JSON typed paths. Consult them when
choosing the target type for a specific column. The notes below are limited
to **Snowflake-specific quirks** that those rules don't cover.

| Snowflake | Default ClickHouse mapping | Snowflake-specific note |
|---|---|---|
| `NUMBER(P, S)` (S > 0) | `Decimal(P, S)` | — |
| `NUMBER(P, 0)` (integer-like) | `Int*` / `UInt*` | Snowflake doesn't distinguish integer width; pick the smallest CH type that fits the actual max value |
| `FLOAT` / `DOUBLE` | `Float64` | — |
| `TIMESTAMP_NTZ` | `DateTime` or `DateTime64(_)` | Naive local time — no timezone parameter on the CH side |
| `TIMESTAMP_TZ` | `DateTime64(_, 'UTC')` | Convert at the source: `CONVERT_TIMEZONE('UTC', col)`. Snowflake stores `TIMESTAMP_TZ` at nanosecond precision by default — verify actual data precision before picking the CH scale |
| `TIMESTAMP_LTZ` | `DateTime64(_, 'UTC')` | Session-local in Snowflake; always normalize to UTC at the source |
| `DATE` | `Date` / `Date32` | — |
| `TIME` | `String` | ClickHouse has no native TIME |
| `VARIANT` | `JSON` | `snowflake-connector` returns VARIANT as a JSON string (already serialized) — insert directly, don't re-parse |
| `OBJECT` | `JSON` | Same as VARIANT |
| `ARRAY` | `Array(T)` if homogeneous, else `JSON` | Snowflake ARRAY can be heterogeneous — sample before deciding |
| `BOOLEAN` | `Bool` | — |
| `VARCHAR(n)` | `String` | The `(n)` length cap is informational in Snowflake and not enforced in CH; ignore it |
| `BINARY` | `String` / `FixedString(N)` | — |
| `GEOGRAPHY` / `GEOMETRY` | `String` (WKT) or `Point` / `Polygon` / `Ring` | No native CH GEOGRAPHY type — verify the right `String`-vs-typed choice in the CH docs |

For every column, run `SELECT COUNT(*), COUNT(<col>), COUNT(DISTINCT <col>)`
before committing — the CH best-practices rules tell you what to do with those
numbers.

---

## Snowflake-Specific Objects — Migration Patterns

For each Snowflake-specific object discovered in the source schema, the
agent decides (a) recreate, (b) replace with a different mechanism, or
(c) defer / flag as out-of-scope. Document the decision in the final report.

### Streams (CDC)

No direct ClickHouse equivalent. Options:
- **Defer.** Migrate the underlying table; document the stream as
  out-of-scope. Recommend ClickPipes for ongoing CDC on the ClickHouse side.
- **Replace** with a `ReplacingMergeTree` + version column on the target,
  fed by the partner's ingestion pipeline.

Look up `streams-intro.md` to understand exactly what change-tracking
semantics the source stream is providing before deciding.

### Tasks (scheduled SQL)

No equivalent in ClickHouse itself. Reimplement with an external scheduler
(Airflow, dbt, cron) that invokes the `clickhousectl` MCP or runs the SQL
via the ClickHouse Cloud HTTPS endpoint. Document the task graph in the
final report.

### Dynamic Tables

Snowflake's declarative materializations with auto-refresh. Translate to a
ClickHouse Materialized View targeting an `AggregatingMergeTree` (for
GROUP BY / aggregate workloads) or `SummingMergeTree` (for simple sums).

**Always generate the `CREATE MATERIALIZED VIEW` AND the backfill `INSERT`
together** — creating the MV without backfilling leaves a gap in historical
data. (This rule comes from the base ClickHouse prompt and applies here.)

Look up `dynamic-tables-about.md` to understand the source's `TARGET_LAG`
and refresh-mode semantics before designing the MV.

### Snowflake Materialized Views

Different semantics from ClickHouse MVs — Snowflake MVs auto-refresh,
ClickHouse MVs are insert-time triggers. Recreate as a ClickHouse MV with
explicit `POPULATE` or with a separate backfill INSERT.

### Time Travel (`AT(TIMESTAMP => ...)` / `BEFORE(STATEMENT => ...)`)

No equivalent in ClickHouse. Flag any reliance on Time Travel as a feature
gap in the migration report.

### Iceberg Tables

ClickHouse has the `iceberg()` table function — read-only passthrough to
the same Iceberg storage. The migration is usually just "point ClickHouse
at the same Iceberg catalog."

### Secure Views / Row-Access Policies

Recreate as ClickHouse row policies (`CREATE ROW POLICY`) or as filtered
views on the target. Inspect the policy expression and convert to ClickHouse
SQL syntax.

### Hybrid Tables (OLTP)

No ClickHouse equivalent — ClickHouse is OLAP-only. Out of scope; flag in
the report and recommend keeping these workloads in Snowflake or moving
to a separate OLTP store.

### Clustering Keys

Translate to ClickHouse `ORDER BY (...)`. Column order matters — the leading
column wins prefix filter pruning. The Snowflake clustering key is a strong
hint but verify the actual query patterns before copying it verbatim.

### Search Optimization Service

Replace with appropriate ClickHouse skip indexes (`minmax`, `set`, Bloom
filter, `ngrambf_v1` for substring search). Inspect WHERE clauses to pick
the right index type.

### Snowpark / UDFs (Python, Java, Scalar SQL)

Rewrite scalar SQL UDFs as ClickHouse SQL UDFs (`CREATE FUNCTION ... AS ...`).
For Python/Java UDFs, the options are: rewrite as SQL, embed in the
application layer, or compile a ClickHouse C++ UDF (high cost).

---

## Python Script Conventions

When generating Python for `migration-runner`, follow these rules.

**Snowflake connection** — read from env, use qmark paramstyle:

```python
import os, snowflake.connector

sf = snowflake.connector.connect(
    account    = os.environ["SNOWFLAKE_ACCOUNT"],
    user       = os.environ["SNOWFLAKE_USER"],
    password   = os.environ["SNOWFLAKE_PASSWORD"],
    warehouse  = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
    paramstyle = "qmark",
)
```

The database and schema vary by workload — set them per query or via
`USE SCHEMA` after connecting.

**ClickHouse Cloud connection** — same pattern as the postgres rules:

```python
import clickhouse_connect
ch = clickhouse_connect.get_client(
    host     = os.environ["CLICKHOUSE_CLOUD_HOST"],
    port     = 8443,
    username = os.environ.get("CLICKHOUSE_CLOUD_USER", "default"),
    password = os.environ["CLICKHOUSE_CLOUD_PASSWORD"],
    database = os.environ.get("CLICKHOUSE_CLOUD_DATABASE", "migration_target"),
    secure   = True,
    verify   = False,
)
```

**Always alias every function expression in SELECT.** Cursor row dicts key by
the SQL output name. `COALESCE(col, default)` produces key `'COALESCE'`,
not `'col'`. Every non-trivial expression needs an explicit `AS alias`
matching the target column name.

**Never duplicate column names** in the `column_names` list passed to
`client.insert`. If the target has a column the source does not, use a
SELECT alias — never repeat a name.

**Batch by date range or ID range** for tables over ~500k rows. Always
query the actual range first (`SELECT MIN, MAX, COUNT`) and generate every
batch in one script — never one batch at a time.

**Verify row counts at the end**: compare source `COUNT(*)` against target
`count()` per table. Raise an exception if they don't match.
