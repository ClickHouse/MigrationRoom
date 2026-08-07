# Step 6 — Optimize the slow queries

Step 5 produced a per-query comparison. Take the queries where ClickHouse
Cloud did **not** win, or won by less than you'd expect, and improve them.

This step is chat reasoning plus DDL through `clickhousectl` — no
`migrationkit` script.

1. For each underperforming query, run `EXPLAIN indexes = 1` and read
   which parts and granules were scanned.
2. Diagnose: wrong `ORDER BY` prefix, missing projection, a partition key
   that prunes nothing, a `Nullable` column in the sort key, or a codec
   choice inflating reads.
3. Propose one change at a time, apply it with `run_query`, and re-time
   the query.
4. When you're done, tell the partner to click **Benchmark** again so the
   dashboard records the improved numbers against the same run.

Explain each change in terms of what ClickHouse does differently, not just
what you typed — the partner is evaluating the engine, not the syntax.
