## Databricks Source — Migration Instructions

This section applies when the SOURCE database is Databricks.

---

## Unity Catalog is Three-Level — Always Fully Qualify

Databricks names objects `catalog.schema.table`. ClickHouse has two levels
(`database.table`).

- **Always fully qualify** source tables in generated SQL and in
  `migrationkit` `source_query=` values. A bare `orders` resolves against
  whatever the connection's default namespace happens to be, which may not
  be the namespace the partner selected.
- The playground's "source database" is the `catalog.schema` pair as one
  dotted string (`DATABRICKS_NAMESPACE`, e.g. `migration_demo.tpch`). When
  you see a single dotted value in a prompt, that is what it is.
- Identifiers are case-insensitive and stored lower-case. Quote with
  backticks when a name needs it — never double quotes.

---

## Schema Discovery — Don't Assume Anything

Never assume column names, table names, types, or that any particular
Databricks feature is or is not in use. Discover the real schema at
runtime via the `databricks-source` MCP.

**Use `databricks-source` (not `migration-runner`) for all schema
discovery and read-only inspection.** `migration-runner`'s `run_python` is
**only** for data movement once the schema is understood. Reasons:

- `databricks-source` is workspace-scoped: `list_catalogs` returns every
  catalog the principal can see, not just one default namespace.
- `migration-runner` inherits env vars from the playground's `.env` via
  `env_file:`. A stale `DATABRICKS_NAMESPACE` there silently scopes a
  connector session to the wrong catalog/schema and your inventory
  quietly misses everything else.
- The MCP is guarded read-only, so introspection cannot mutate the source
  even by accident.

**The discovery checklist for every Databricks migration:**

1. **List catalogs and schemas:**
   `list_catalogs()` then `list_schemas(catalog)`.

2. **List tables with sizes:**
   `list_tables(catalog, schema)` — returns `table_type`, `comment`,
   `sizeInBytes`, `numFiles`. It deliberately does **not** return row
   counts: Delta metadata doesn't carry them and a per-table `COUNT(*)`
   would make the call slow.

3. **Get row counts in ONE query**, not one call per table:
   ```sql
   SELECT 'orders' AS t, count(*) AS n FROM <catalog>.<schema>.orders
   UNION ALL SELECT 'lineitem', count(*) FROM <catalog>.<schema>.lineitem
   ORDER BY n DESC
   ```

4. **Full schema plus Delta detail per table:**
   `describe_table(catalog, schema, table)`. Read all three sections of
   the response — `describe_extended` for columns, `detail` for
   clustering/partition columns and table features, `history` for whether
   the table is actively mutated. Clustering columns and deletion-vector
   state live only in `detail`.

5. **Inventory the source-specific features you actually found:**
   ```sql
   -- VARIANT / nested columns, generated columns, identity columns
   SELECT column_name, full_data_type, is_nullable, generation_expression
   FROM system.information_schema.columns
   WHERE table_catalog = '<catalog>' AND table_schema = '<schema>'
   ORDER BY table_name, ordinal_position
   ```
   ```sql
   -- Materialized views and streaming tables are separate object types;
   -- a table listing alone misses them.
   SELECT table_name, table_type
   FROM system.information_schema.tables
   WHERE table_catalog = '<catalog>' AND table_schema = '<schema>'
   ```

6. **Sample data and check nullability + cardinality** for every column
   before choosing types. This is what tells you whether a column is
   `Nullable(T)`, whether a string is a `LowCardinality(String)`
   candidate, and whether a `DECIMAL` needs full precision:
   ```sql
   SELECT * FROM <catalog>.<schema>.<table> LIMIT 5
   SELECT count(*), count(<col>), count(DISTINCT <col>) FROM <catalog>.<schema>.<table>
   ```

7. **Read the partner's queries.** `ORDER BY` key selection on the
   ClickHouse side comes from the columns in WHERE / JOIN / GROUP BY of
   the actual workload — **not** from the source's `CLUSTER BY` columns.

Produce a migration inventory before generating any target schema.

---

## Databricks → ClickHouse Type Mapping

| Databricks | ClickHouse | Notes |
|---|---|---|
| `BOOLEAN` | `Bool` | |
| `TINYINT` / `SMALLINT` / `INT` / `BIGINT` | `Int8` / `Int16` / `Int32` / `Int64` | Databricks integers are always signed |
| `FLOAT` / `DOUBLE` | `Float32` / `Float64` | |
| `DECIMAL(p, s)` | `Decimal(p, s)` | **Never** `Float64` — TPC-H money columns need exact arithmetic |
| `STRING` | `String`, or `LowCardinality(String)` | Use `LowCardinality` below ~10k distinct values |
| `BINARY` | `String` | |
| `DATE` | `Date32` | `Date` only reaches 2149 and starts at 1970 |
| `TIMESTAMP` | `DateTime64(6, 'UTC')` | Databricks `TIMESTAMP` is instant-with-timezone; normalise to UTC at the source |
| `TIMESTAMP_NTZ` | `DateTime64(6)` | No timezone — do **not** attach one |
| `INTERVAL` | `Int64` seconds, or `String` | No native equivalent; state the unit in a comment |
| `ARRAY<T>` | `Array(T)` | |
| `MAP<K, V>` | `Map(K, V)` | Missing-key lookup returns the type default, not NULL |
| `STRUCT<a: T, …>` | named `Tuple(a T, …)` | Positional in ClickHouse — field order matters |
| `ARRAY<STRUCT<…>>` | `Nested(...)` or `Array(Tuple(...))` | `Nested` is easier to query; `Array(Tuple)` is easier to insert |
| `VARIANT` | `JSON`, or hot keys extracted to typed columns | Extracting the 2–3 keys the workload filters on usually beats a whole `JSON` column |
| generated column | `MATERIALIZED` or `ALIAS` | `MATERIALIZED` stores it, `ALIAS` recomputes on read |
| identity column | `Int64` + no auto-generation | ClickHouse has no identity; the migrated values are what you keep |

**Nullability:** `system.information_schema.columns.is_nullable` is
authoritative. Either declare `Nullable(<T>)` on the target, or declare
non-Nullable with an explicit `DEFAULT` **and** map `None` to that default
in a step-2 `transform=`. A non-Nullable target column with neither fails
mid-batch on the first NULL.

---

## Delta / Unity Catalog Objects — Migration Patterns

### Liquid clustering (`CLUSTER BY`)
Not an index and not a sort order you can copy. Treat it as a hint about
which columns the workload filters on, then choose the ClickHouse
`ORDER BY` from the actual queries. Say in chat when your choice differs
from the source clustering and why.

### Deletion vectors
Deletes are recorded as vectors rather than rewritten files, so a table can
report rows that a `SELECT` won't return. Two consequences: source
`COUNT(*)` is the only trustworthy count (never sum file statistics), and a
table being actively deleted from can legitimately change count mid-run.

### Time travel (`VERSION AS OF` / `TIMESTAMP AS OF`)
No ClickHouse equivalent. Use it to pin a consistent read for the
migration itself (`SELECT * FROM t VERSION AS OF <v>`) so a concurrent
write doesn't skew validation. Do not try to reproduce the history.

### Materialized views and streaming tables
Recreate as a ClickHouse Materialized View over an `AggregatingMergeTree`,
and **backfill** it from the base table after loading — an MV only sees
rows inserted after it exists. Get the defining query from
`describe_table`'s extended output.

### Change Data Feed (`delta.enableChangeDataFeed`)
Ongoing CDC is out of scope for a one-shot migration. If the partner needs
it, the answer is ClickPipes or a `ReplacingMergeTree` with a version
column — say so and move on rather than half-building it.

### Unity Catalog volumes and external locations
Only relevant to the S3 staging path. A `/Volumes/...` path is not
readable by ClickHouse; the staged unload writes to `s3://` directly.

---

## Migration Script Rules

- Connect with `DatabricksSource.from_env()`. Never construct
  `databricks.sql.connect(...)` by hand in a generated script — the
  helper owns namespace splitting and host normalisation.
- Fully qualify every `source_query=`.
- Row dict keys are **lower-case**; `iter_batches` lowercases them.
- **Complex types need no `transform=`.** The framework already delivers
  values in the shape ClickHouse wants, so do not write conversion code for
  them — verified end-to-end against `lineitem` and `orders` with no
  transform at all:

  | Databricks | arrives as | goes into |
  |---|---|---|
  | `ARRAY<T>` | `list` (never a numpy array) | `Array(T)` |
  | `ARRAY<STRUCT<…>>` | list of dicts | `Array(Tuple(named…))` — dicts are accepted |
  | `MAP<K,V>` | `dict` | `Map(K,V)` |
  | `VARIANT` | JSON `str` | `String` or `JSON` |
  | `DECIMAL` | `Decimal` | `Decimal(p,s)` |

  So `if row["some_array"]:` is safe, and you need neither `.tolist()` nor
  dict→tuple rewriting nor `json.dumps`.
- **Never delete a generated column from the row.** `ALIAS` and
  `MATERIALIZED` target columns are excluded from the insert automatically,
  and ClickHouse computes them itself. Writing `del row["o_orderyear"]` is
  unnecessary.
- Use `transform=` only for genuine value-level changes — mapping `None` to a
  non-Nullable column's default, rescaling a unit, redacting a field. If your
  transform only reshapes a container or drops a generated column, delete it.
- `batch_size` ~100k narrow, ~25–50k for rows carrying VARIANT or
  `ARRAY<STRUCT>`. Never above 500k.
- **S3 staging is only available when `STAGING_S3_BUCKET` is set.** Check it
  before planning a staged table — with it unset, `S3Stage.from_env()` gives
  you nothing and every table goes down the direct path. Never tell the
  partner a table "is using the S3 staging path" unless you have confirmed
  the variable is set; report the path the run actually took, not the one
  your script would have preferred.
- **S3 staging also requires a Unity Catalog external location** over the
  staging bucket with `WRITE FILES` granted. `unload_to_s3` raises a
  message saying exactly that; fall back to `add_table()` per table.
- **Target column order must match the source** for staged tables — that
  path is `INSERT INTO … SELECT * FROM s3(...)`, which is positional.
- The staged path supports neither `batch_size` nor `transform`. Any
  table needing per-row transformation uses the direct path.

---

## Query Rewriting Notes

| Databricks | ClickHouse |
|---|---|
| `QUALIFY <pred>` | subquery + `WHERE` over the window |
| `LATERAL VIEW explode(arr) AS x` | `ARRAY JOIN` / `arrayJoin(arr)` |
| `transform(arr, x -> f(x))` | `arrayMap(x -> f(x), arr)` (order flips) |
| `filter(arr, x -> p(x))` | `arrayFilter(x -> p(x), arr)` (order flips) |
| `aggregate(arr, 0, (a, x) -> a + x)` | `arraySum(arr)` / `arrayReduce('sum', arr)` |
| `v:a.b` / `variant_get(v, '$.a')` | `JSONExtract*(v, 'a')`, or `v.a` on a `JSON` column |
| `named_struct('a', 1)` | `tuple(1)` — positional |
| `try_divide(a, b)` | `if(b = 0, NULL, a / b)` — `/` returns `inf`, not NULL |
| `try_cast(x AS t)` | `accurateCastOrNull(x, 't')` |
| `datediff(a, b)` | `dateDiff('day', b, a)` — order flips |
| `date_trunc('month', d)` | `toStartOfMonth(d)` |
| `SEMI JOIN` / `ANTI JOIN` | `LEFT SEMI JOIN` / `LEFT ANTI JOIN` |
| `catalog.schema.table` | `database.table` |

State `NULLS FIRST` / `NULLS LAST` explicitly — the engines' defaults differ.

---

## Known Gotchas

- **Cold warehouse.** A stopped SQL warehouse takes 30s+ to serve its
  first statement. Never present that as query latency; run a throwaway
  `SELECT 1` before benchmarking.
- **`samples` catalog is read-only.** Copy out of it, never into it.
- **`VARIANT` needs DBSQL 2024.35+ / DBR 15.3+.** On older runtimes the
  column type simply doesn't exist.
- **`system.query.history` can lag** by minutes and may be disabled, so
  `server_ms` is sometimes `None`. Report wall-clock as wall-clock.
- **Auto-stopped warehouse mid-migration.** A long direct-path migration
  keeps the connection busy, but a paused run can let the warehouse stop;
  resuming pays cold-start again. Expected, not a bug.
