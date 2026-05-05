# Prompt 03 — ClickHouse Schema Design

You've now seen the full source schema and the query patterns. Design an optimised
ClickHouse schema for the `migration_target` database.

For each of the 8 tables, make and explain your choices:
- Which MergeTree engine variant, and why
- Which columns to put in ORDER BY, and in what order — show your reasoning
- Whether to add PARTITION BY, and why or why not
- How to handle any Postgres-specific types or features (JSONB, arrays, ENUMs, etc.)
- Any columns where the default value strategy matters

I want to understand the thinking behind each decision, not just the SQL.

Once the design is agreed, create the database and execute all the CREATE TABLE
statements on clickhouse-cloud. Verify each table was created successfully.
