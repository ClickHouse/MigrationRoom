# Step 1 — Discover the source and design the ClickHouse Cloud target schema

You are migrating from `{source}` to ClickHouse Cloud.

- **Source namespace** (where the data lives today): `{database}` — a
  Unity Catalog `catalog.schema` pair, selected by the partner in the
  dashboard. Use it as-is.
- **Target database** (where the data will land in ClickHouse Cloud): not
  chosen yet. Propose a name in this step and confirm with the partner.

If the partner has told you in this conversation to use a different
namespace, follow their chat instruction instead.

## Source

Use the `databricks-source` MCP — **not** `run_python`. Its five tools are
`list_catalogs`, `list_schemas`, `list_tables`, `describe_table`, and
`run_select_query`.

1. `list_tables(catalog, schema)` for the namespace above. It returns
   `sizeInBytes` and `numFiles` per table but **no row counts** — Delta
   metadata doesn't carry them.
2. Get row counts in ONE query rather than one per table:
   ```sql
   SELECT 'orders' AS t, count(*) AS n FROM migration_demo.tpch.orders
   UNION ALL SELECT 'lineitem', count(*) FROM migration_demo.tpch.lineitem
   -- … one line per table
   ORDER BY n DESC
   ```
3. `describe_table(catalog, schema, table)` for every table. Read the
   `detail` and `history` sections too, not just the columns — that is
   where clustering columns, partition columns, table features, and
   deletion-vector state live.
4. Inventory the Databricks-specific features you actually find. Do not
   assume any are present: VARIANT columns, `ARRAY<STRUCT>` / `MAP` /
   `STRUCT`, generated columns, liquid clustering (`CLUSTER BY`),
   deletion vectors, `TIMESTAMP` vs `TIMESTAMP_NTZ`, materialized views,
   streaming tables.
5. Sample rows and check cardinality before designing types:
   ```sql
   SELECT * FROM <catalog>.<schema>.<table> LIMIT 5
   SELECT count(*), count(<col>), count(DISTINCT <col>) FROM <catalog>.<schema>.<table>
   ```
6. Identify fact vs dimension tables and the join graph.

## Analytical workload

The partner will run these against the migrated data. Use them to choose
`ORDER BY` keys, partitioning, and projections — the ordering should come
from the columns in WHERE / JOIN / GROUP BY here, **not** from the source's
clustering columns:

```sql
{olap_queries}
```

## Target

Use the `clickhousectl` MCP to:

1. Create the target database (suggested default `migration_demo`; confirm
   first).
2. `CREATE TABLE` for every source table, following the ClickHouse Cloud
   best-practice rules attached to **clickhousectl**. Justify each engine,
   `ORDER BY`, `PARTITION BY`, and codec choice in chat.
3. Map Databricks types — the full table is in your Databricks source
   instructions. The decisions worth surfacing to the partner:
   - `VARIANT` → `JSON`, or extract hot keys into typed columns
   - `ARRAY<STRUCT<…>>` → `Nested(...)` or `Array(Tuple(...))`
   - `MAP<STRING, STRING>` → `Map(String, String)`
   - `DECIMAL(p, s)` → `Decimal(p, s)`, never `Float64`
   - `TIMESTAMP` → `DateTime64(6, 'UTC')`; `TIMESTAMP_NTZ` → `DateTime64(6)`
   - generated column → `MATERIALIZED` (stored) or `ALIAS` (computed)
4. **Column order must match the source table's column order** for every
   table you plan to migrate through S3 staging in step 2 — that path does
   `INSERT INTO … SELECT * FROM s3(...)`, which is positional.
5. **Handle nullable columns deliberately.** `describe_table` reports
   nullability. For each nullable column either declare
   `Nullable(<T>)`, or declare it non-Nullable with an explicit `DEFAULT`
   AND add a `transform=` lambda in step 2 mapping `None` to that default.
   A non-Nullable column with neither will fail mid-batch on the first NULL.
6. Verify with `SHOW TABLES`.

## When you're done

Summarise the source namespace, target database name, and the key schema
decisions in chat — later steps refer back to them. Do **not** insert any
data; that is step 2.
