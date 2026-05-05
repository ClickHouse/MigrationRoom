# Migration Checklist

Use this with the AI agent to track progress through the Postgres → ClickHouse migration.

## Phase 1 — Schema Analysis
- [ ] Source schema explored (all 8 tables documented)
- [ ] Data types catalogued — JSONB, arrays, ENUMs, TIMESTAMPTZ flagged
- [ ] Query patterns analysed — WHERE/GROUP BY/JOIN columns identified
- [ ] Postgres-specific features listed for translation

## Phase 2 — ClickHouse Schema Design
- [ ] Engine selected per table (MergeTree / ReplacingMergeTree)
- [ ] ORDER BY keys designed (low→high cardinality, matches query filters)
- [ ] Partitioning defined (toYYYYMM for all time-series tables)
- [ ] Data types mapped:
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
- [ ] All 8 CREATE TABLE statements executed and verified

## Phase 3 — Data Migration
- [ ] postgresql() table function tested for each source table
- [ ] Dimension tables migrated first: users, products
- [ ] Session table migrated
- [ ] Fact tables migrated: events (batch by month if needed), orders, order_items
- [ ] Ad impressions migrated
- [ ] Inventory snapshots migrated
- [ ] Row count validation passed for all 8 tables
- [ ] NULL / default value handling verified

## Phase 4 — Query Migration
- [ ] All 10 sample queries rewritten for ClickHouse syntax
- [ ] Key syntax differences noted: countIf, WITH FILL, quantile, uniq
- [ ] Performance comparison run (Postgres vs ClickHouse timing)
- [ ] EXPLAIN output reviewed for primary key utilisation

## Phase 5 — Optimisation
- [ ] Materialized Views created for top 3 heaviest aggregations
- [ ] ClickPipes evaluated for ongoing Postgres replication (if applicable)
- [ ] Schema compared against reference: queries/expected_ch_schema.sql
