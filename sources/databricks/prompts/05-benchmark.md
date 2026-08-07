# Step 5 — Benchmark source vs target

Run the same analytical workload on Databricks and on ClickHouse Cloud and
compare server-side execution time.

`Benchmarker` pairs each source query with its target rewrite, times both,
and writes the `benchmarks` rows the dashboard's Benchmark tab reads.

```python
from migrationkit import Benchmarker, DatabricksSource, ClickHouseTarget

b = Benchmarker(
    run_id="<run_id-from-step-2>",
    source=DatabricksSource.from_env(),
    target=ClickHouseTarget.from_env(),
    target_database="<target-db-from-step-1>",
)
result = b.benchmark(queries=[
    (
        "<databricks query 1>",
        "<clickhouse query 1>",
    ),
    # … one (source_sql, target_sql) pair per query from step 4
])
print(result)
```

Dispatch with `run_python_background` if the workload has more than a
couple of queries (a cold Databricks warehouse can take 30s+ on the first
statement), then ONE `tail_python_job` to confirm it started. Otherwise
`run_python` is fine.

## Rules

- Use the **same `run_id`** as step 2 so the dashboard associates the
  benchmark with the migration.
- Pass the step-4 rewrites verbatim. Benchmarking a hand-tuned target
  query against an untuned source query is not a comparison.
- Fully qualify the Databricks side (`catalog.schema.table`).
- **The first Databricks query in a session pays warehouse start-up cost.**
  Run one throwaway `SELECT 1` first, or say plainly that query 1's number
  includes cold-start.
- `server_ms` for Databricks comes from the SQL query-history API, with
  `system.query.history` as a fallback. If both are unavailable it is
  `None` and the dashboard shows wall-clock instead — say so rather than
  presenting wall time as server time.

## When you're done

Report the per-query comparison and the aggregate speed-up, and point at
the Benchmark tab. Be honest about queries where ClickHouse is slower —
those are step 6's input.
