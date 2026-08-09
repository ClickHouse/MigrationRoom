# Step 2 — Migrate data using `migrationkit`

The target schema from step 1 is in place. Copy the data from Databricks
into ClickHouse Cloud with the `migrationkit` library — it handles
batching, per-batch checkpointing, pause/resume/cancel, and the live
progress events the dashboard renders.

## Pick a path per table

| Path | Use when | API |
|---|---|---|
| **Direct** | `total_rows ≤ 1_000_000` | `m.add_table(...)` |
| **S3 staging** | `total_rows > 1_000_000` AND `STAGING_S3_BUCKET` is set | `m.add_table_via_s3(name=..., stage=S3Stage.from_env())` |

```python
import os
USE_S3 = bool(os.environ.get("STAGING_S3_BUCKET"))
```

If `USE_S3` is False, use the direct path for every table and note in chat
that the partner hasn't configured S3 staging. Don't fail the migration —
direct works at any size, it's just slower.

**The S3 path additionally needs a Unity Catalog external location** over
the staging bucket with `WRITE FILES` granted. If `unload_to_s3` raises,
the error says so; fall back to `add_table()` for that table and tell the
partner.

## What to write

One Python script (~25 lines), dispatched with `run_python_background`,
confirmed with ONE `tail_python_job` call.

```python
import os
import time
from migrationkit import Migrator, DatabricksSource, ClickHouseTarget, S3Stage

USE_S3 = bool(os.environ.get("STAGING_S3_BUCKET"))
stage = S3Stage.from_env() if USE_S3 else None

m = Migrator(
    run_id=f"migrate-databricks-{int(time.time())}",
    source=DatabricksSource.from_env(),
    target=ClickHouseTarget.from_env(),
    # REQUIRED: the ClickHouse Cloud database from step 1.
    target_database="<target-db-from-step-1>",
)

# Direct path: dimensions and small facts. `target_table` is a BARE name —
# never `db.table`; the Migrator owns the database via target_database=.
m.add_table(
    name="<dim_table>",
    source_query="SELECT * FROM <catalog>.<schema>.<dim_table>",
    target_table="<dim_table>",
    batch_size=100_000,
)

# S3-staged path: large facts, only when stage is set.
if stage is not None:
    m.add_table_via_s3(name="<fact_table>", target_table="<fact_table>", stage=stage)
else:
    m.add_table(
        name="<fact_table>",
        source_query="SELECT * FROM <catalog>.<schema>.<fact_table>",
        target_table="<fact_table>",
        batch_size=50_000,
    )

# … one m.add_table(...) or m.add_table_via_s3(...) per source table.

m.run()
```

Chat-side flow:

```text
1.  call: write_workspace_file(path="migrate.py", content=<script above>)
2.  call: run_python_background(code=<the script>)            ← capture job_id
3.  call: tail_python_job(job_id=..., max_wait_seconds=5)     ← ONE call, confirm status=running
4.  reply in chat: "Migration <run_id> is running — watch the dashboard."
```

**No polling loop after that.** The dashboard streams progress over SSE.

## Rules

- Always pass `target_database=` to `Migrator(...)` and bare names to
  `target_table`. A qualified `db.table` raises `ValueError` at
  registration.
- Source queries must be **fully qualified** (`catalog.schema.table`).
  Unity Catalog is three-level and the connection's default namespace may
  not be what you assume.
- **Row dict keys are lowercase** — `iter_batches` lowercases column
  names. Write any `transform=` lambda in lowercase.
- `batch_size`: ~100k for narrow tables, ~25–50k for tables carrying
  VARIANT or `ARRAY<STRUCT>` columns. Never above 500k.
- VARIANT values arrive as Python strings or dicts depending on the
  connector version. `json.dumps(...)` them if the target column is
  `String`; pass through unchanged if it is `JSON`.
- `ARRAY<STRUCT>` arrives as a list of dicts. For a ClickHouse `Nested`
  target you must pivot it into parallel arrays in a `transform=`; for
  `Array(Tuple(...))` convert each dict to a tuple in field order.
- S3 staging supports neither `batch_size` nor `transform` — it is
  `INSERT OVERWRITE DIRECTORY` → `INSERT FROM s3()`. Any table needing
  per-row transformation must use the direct path.
- If `S3Stage.from_env()` raises a missing-env error, tell the partner to
  set `STAGING_S3_*` (see `docs/object-storage-staging.md`) or accept the
  direct path.

## When you're done

Say which path each table took and why, tell the partner the migration is
**running** (not "complete"), and point at the dashboard. Step 3 runs when
they click it.
