# Migration Checklist

A phase-by-phase checklist partners can use with the AI migration agent
to track progress on a source → ClickHouse Cloud migration. The phases
map 1:1 to the dashboard's six steps and to each source's
`sources/<source>/prompts/01..06-*.md` files.

This checklist is shared across all five supported sources (Snowflake,
BigQuery, Databricks, Postgres, ClickHouse OSS). The type-mapping table in Phase 2
is written with **Postgres-specific** types as a concrete example;
other source engines have analogous mappings — see the per-source
prompts and `librechat/sources/<source>-instructions.md` for the
authoritative lists.

---

## Phase 1 — Schema Analysis
- [ ] Source schema explored (all source tables documented)
- [ ] Data types catalogued — semi-structured types, arrays, timestamps, ENUMs flagged
- [ ] Query patterns analysed — WHERE / GROUP BY / JOIN columns identified
- [ ] Source-engine-specific features listed for translation
      (VARIANT / Streams / Dynamic Tables for Snowflake; STRUCT / Materialized Views for BigQuery;
       VARIANT / liquid clustering / deletion vectors for Databricks;
       JSONB / arrays / ENUMs for Postgres; AggregatingMergeTree / MVs for CH OSS)

## Phase 2 — ClickHouse Schema Design
- [ ] Engine selected per table (MergeTree / ReplacingMergeTree)
- [ ] ORDER BY keys designed (low→high cardinality, matches query filters)
- [ ] Partitioning defined (toYYYYMM for all time-series tables)
- [ ] Data types mapped — example for **Postgres** (other sources analogous):
  - [ ] BIGSERIAL / SERIAL → UInt64 / UInt32
  - [ ] TIMESTAMPTZ → DateTime64(3, 'UTC')
  - [ ] Low-cardinality VARCHAR → LowCardinality(String)
  - [ ] NUMERIC(p,s) → Decimal(p,s)
  - [ ] BOOLEAN → Bool
  - [ ] UUID → UUID
  - [ ] JSONB → JSON
  - [ ] TEXT[] (arrays) → Array(String)
  - [ ] ENUM → LowCardinality(String) or Enum8/Enum16
- [ ] Nullable columns minimised (use defaults instead)
- [ ] All CREATE TABLE statements executed and verified

## Phase 3 — Data Migration
- [ ] Source-native bulk-read path tested (postgresql() / s3() / table read)
- [ ] Dimension tables migrated first
- [ ] Fact tables migrated (batch by month if needed)
- [ ] Row count validation passed for all source tables
- [ ] NULL / default value handling verified

## Phase 4 — Validate
- [ ] Sample queries rewritten for ClickHouse syntax
- [ ] Key syntax differences noted: `countIf`, `WITH FILL`, `quantile`, `uniq`
- [ ] EXPLAIN output reviewed for primary-key utilisation
- [ ] Query results match the source within acceptable tolerance

## Phase 5 — Benchmark
- [ ] Source vs target query timings captured for the full query set
- [ ] Slow queries flagged for Phase 6 attention
- [ ] Speedup summary reviewed (server-side timing, not wall-clock)

## Phase 6 — Optimise
- [ ] Materialised Views created for the heaviest repeated aggregations
- [ ] ORDER BY / PARTITION BY revisions considered for slow-query tables
- [ ] ClickPipes evaluated for ongoing source replication (if applicable)
- [ ] Final schema compared against any reference solution in
      `sources/<source>/queries/expected_ch_schema.sql`
