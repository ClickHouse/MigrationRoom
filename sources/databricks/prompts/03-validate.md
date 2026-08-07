# Step 3 — Validate row-count parity

The migration from step 2 has finished (the dashboard's Migration tab shows
every table `done`). Confirm the target matches the source before anyone
rewrites a query against it.

`Validator` reads the tables registered in the run's `run_tables` rows, so
you don't re-list them — pass the same `run_id`.

```python
from migrationkit import Validator, DatabricksSource, ClickHouseTarget

v = Validator(
    run_id="<run_id-from-step-2>",
    source=DatabricksSource.from_env(),
    target=ClickHouseTarget.from_env(),
    target_database="<target-db-from-step-1>",
)
result = v.validate()
print(result)
```

This is a short script — dispatch it with `run_python` (not
`run_python_background`); it returns in seconds.

## Rules

- Use the **same `run_id`** as step 2. A new one has no `run_tables` rows
  and validates nothing.
- Do not hand-write per-table `count()` comparisons; `Validator` writes
  the `validations` rows the dashboard's Validation tab reads. Ad-hoc
  counts leave that tab empty.
- If a table mismatches, **stop and report it**. Do not re-run the
  migration for that table without telling the partner — a partial
  re-insert without a `TRUNCATE` first duplicates rows.
- A deliberate caveat for this source: if the source table has deletion
  vectors enabled and rows were deleted after step 2 started, the source
  count can legitimately drop mid-run. Re-check the source count before
  calling it a migration bug.

## When you're done

Report per-table parity in chat and point at the Validation tab. Step 4
(Rewrite Queries) runs when the partner clicks it.
