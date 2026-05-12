# Prompt 06 — Materialized View Optimisation

> Propose Materialized View optimisations for the heaviest aggregation
> queries. For every Snowflake Dynamic Table you found in prompt 01, decide
> whether to recreate it as a ClickHouse MV (and which engine —
> AggregatingMergeTree, SummingMergeTree, etc.). Produce the
> `CREATE MATERIALIZED VIEW` and the backfill `INSERT` together; wait for
> my confirmation before executing.
