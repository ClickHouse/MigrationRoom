## PostgreSQL Source — Migration Instructions

These rules apply when the data source is PostgreSQL.

---

## Data Migration — Batch Large Tables

When migrating a table with more than **500,000 rows**, never generate a single
`INSERT INTO ... SELECT * FROM postgresql(...)` without a WHERE filter.
Large unbatched migrations time out and are hard to resume.

**Before writing any batch, always query the actual data range first:**
```sql
-- For date-based batching:
SELECT MIN(<ts_col>), MAX(<ts_col>), count() FROM postgresql(..., '<table>', ...)

-- For ID-based batching:
SELECT MIN(<id_col>), MAX(<id_col>), count() FROM postgresql(..., '<table>', ...)
```

**Always generate ALL batches covering the complete data range in one script.**
Never generate only Batch 1 or a single example batch — the user must be able to run
the full script to completion without asking for more batches. Number each batch
clearly (`-- Batch 1 of N`) so progress is trackable.

**By date range (preferred when a timestamp column exists):**
```sql
-- Example: large event table batched monthly
-- Batch 1 of N
INSERT INTO target_db.<table> SELECT ... FROM postgresql(..., '<table>')
WHERE <ts_col> >= '2024-01-01' AND <ts_col> < '2024-02-01';
-- Batch 2 of N
INSERT INTO target_db.<table> SELECT ... FROM postgresql(..., '<table>')
WHERE <ts_col> >= '2024-02-01' AND <ts_col> < '2024-03-01';
-- ... continue through all months to MAX(<ts_col>)
```

**By ID range (when no timestamp column exists):**
```sql
-- Example: 1.5M rows, IDs 1–1,500,000 → 3 batches of 500K
-- Batch 1 of 3
INSERT INTO target_db.<table> SELECT ... FROM postgresql(..., '<table>')
WHERE <id_col> >= 1 AND <id_col> <= 500000;
-- Batch 2 of 3
INSERT INTO target_db.<table> SELECT ... FROM postgresql(..., '<table>')
WHERE <id_col> > 500000 AND <id_col> <= 1000000;
-- Batch 3 of 3
INSERT INTO target_db.<table> SELECT ... FROM postgresql(..., '<table>')
WHERE <id_col> > 1000000 AND <id_col> <= 1500000;
```

Batch size guidelines by row count:
- **> 10M rows**: batch by **month**
- **1M – 10M rows**: batch by **quarter** or **500K-row ID chunks**
- **500K – 1M rows**: batch by **500K-row ID chunks**
- **< 500K rows**: single statement, no batching needed

End every migration script with a row count validation:
```sql
SELECT '<table>' AS table_name, count() AS ch_rows FROM target_db.<table>
UNION ALL SELECT '<table2>', count() FROM target_db.<table2>
-- ... one line per migrated table
```

---

## JSON Columns — COALESCE at String Level Before Casting

**JSONB/JSON columns from `postgresql()` arrive as `String`**, not as `JSON` type.
Never put a `CAST(..., 'JSON')` expression inside `COALESCE` alongside a String —
they have no common supertype. Always COALESCE at the String level first, then cast:

```sql
-- WRONG: String vs JSON inside COALESCE → NO_COMMON_TYPE error
COALESCE(json_col, CAST('{}', 'JSON'))

-- CORRECT: COALESCE both as String, cast the result
CAST(COALESCE(json_col, '{}'), 'JSON')
```

---

## Enum Columns — Always Verify Distinct Values Before Defining Enum8

Never define a ClickHouse `Enum8`/`Enum16` based on the Postgres schema definition alone.
Postgres ENUM type definitions can be outdated; the actual data often contains values
not listed in the type (added via `ALTER TYPE ... ADD VALUE`).

**Rule:** before writing any `Enum8(...)` in a CREATE TABLE, query the source:
```sql
-- Run this via postgres-source MCP before designing the column
SELECT DISTINCT <enum_col> FROM <table> ORDER BY 1;
```

If the column has unknown/unexpected values at migration time, the INSERT will fail with
`Unknown element '...' for enum`. Prefer `LowCardinality(String)` for status-like columns
in migration schemas — same compression and query performance, no enum drift risk:

```sql
-- Fragile: breaks if data contains any unlisted value
status Enum8('active' = 1, 'inactive' = 2)

-- Robust: handles any string value, same performance
status LowCardinality(String)
```

Use `Enum8` only when the value set is truly closed and controlled (e.g., a column
you own end-to-end). For migrated columns from external systems, default to
`LowCardinality(String)`.

---

## Type Coercion — COALESCE and Default Values

ClickHouse enforces strict type matching inside `COALESCE`. All arguments must share
a common supertype. Mixing `Decimal` with `Float64` (or `Nullable(T)` with a wrong
literal) raises `NO_COMMON_TYPE` at query time.

Rules when writing `COALESCE(col, fallback)` in migration SELECT statements:

| Source column type | Correct fallback form |
|---|---|
| `Decimal(P, S)` | `toDecimal64(0, S)` or `CAST(0 AS Decimal(P, S))` |
| `Float32 / Float64` | `0.0` (Float64 literal — fine as-is) |
| `Int* / UInt*` | `0` |
| `String` | `''` |
| `Array(T)` | `[]` — but cast if T is not inferred: `CAST([], 'Array(T)')` |
| `JSON` | `CAST('{}', 'JSON')` — not a String literal |
| `DateTime / Date` | `toDateTime(0)` / `toDate(0)` |
| `Nullable(T)` | Use `assumeNotNull(col)` if NULL is truly impossible, otherwise keep Nullable |

Never use a bare numeric literal as the fallback for a `Decimal` column.
Always inspect the target schema column type before writing the COALESCE default.

---

## Python Migration Scripts — PostgreSQL Source

When generating or adapting Python scripts to migrate from PostgreSQL, follow these rules.

**PostgreSQL connection** — read from environment variables:
```python
PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = int(os.getenv("PG_PORT", "5432"))
PG_USER     = os.getenv("PG_USER", "")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")
PG_DB       = os.getenv("PG_DB", "")
```

**ClickHouse Cloud connection** — read from environment variables:
```python
CH_HOST     = os.getenv("CLICKHOUSE_CLOUD_HOST", "")
CH_PORT     = int(os.getenv("CLICKHOUSE_CLOUD_PORT", "8443"))
CH_USER     = os.getenv("CLICKHOUSE_CLOUD_USER", "default")
CH_PASSWORD = os.getenv("CLICKHOUSE_CLOUD_PASSWORD", "")
CH_DB       = os.getenv("CLICKHOUSE_CLOUD_DATABASE", "migration_target")
```

**SSL certificate verification** — always pass `verify=False` to avoid macOS cert chain errors:
```python
client = clickhouse_connect.get_client(
    host=CH_HOST, port=8443,
    username=CH_USER, password=CH_PASSWORD,
    database=CH_DB,
    secure=True,
    verify=False,   # required on macOS — connection is still TLS-encrypted
)
```

**Always alias every function expression in SELECT** — `DictCursor`/`RealDictCursor` keys
rows by the SQL output name. `COALESCE(col, default)` produces key `'coalesce'`, not `'col'`,
causing a `KeyError` in the sanitize loop. Every non-trivial expression needs an explicit
`AS` alias matching the target column name:
```python
# WRONG — DictCursor row has key 'coalesce', not 'col_name'
"SELECT COALESCE(col_name, '') FROM t"

# CORRECT — row has key 'col_name'
"SELECT COALESCE(col_name, '') AS col_name FROM t"
```
This applies to COALESCE, CAST, arithmetic, and any expression that is not a plain
bare column reference.

**JSONB columns** — psycopg2 returns JSONB as Python dicts. Serialize to string before
inserting into ClickHouse JSON-type columns:
```python
if isinstance(val, dict):
    val = json.dumps(val, default=str)
```

**Array columns** — Postgres array types (e.g. `text[]`) arrive as Python lists, but
elements inside the list can be `None`. clickhouse-connect raises
`TypeError: object of type 'NoneType' has no len()` on `None` elements.
Sanitize before inserting:
```python
elif isinstance(val, list):
    val = ['' if x is None else str(x) for x in val]
```

**Never duplicate column names** — ClickHouse raises `DUPLICATE_COLUMN` if the same name
appears more than once in `column_names`. If the target table has a column the source
does not, use a SELECT alias (e.g. `created_at AS updated_at`) — never repeat a
column name in the list.

---

## postgresql() Table Function — Always Disable SSL for ngrok Tunnels

The ClickHouse `postgresql()` function negotiates SSL by default. ngrok TCP tunnels
are raw TCP — they do not terminate TLS. This causes every connection attempt to fail
with `received invalid response to SSL negotiation`.

Always pass `sslmode=disable` as the 7th argument when the Postgres host is a ngrok address:

```sql
-- Signature:
-- postgresql(host:port, database, table, user, password, schema, connection_string)

-- Correct form for ngrok:
FROM postgresql(
    '<ngrok-host>:<port>',
    '<database>',
    '<table>',
    '<user>',
    '<password>',
    '',                  -- schema (empty = public)
    'sslmode=disable'    -- required for ngrok TCP tunnels
)
```

Apply this to every `postgresql()` call in the migration script — SELECT, INSERT INTO ... SELECT,
and any table function used for row count checks.
