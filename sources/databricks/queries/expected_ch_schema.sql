-- Reference target schema — Databricks migration_demo.tpch → ClickHouse Cloud.
--
-- This is the schema a good agent should arrive at during Step 1 (Discover
-- & Design Schema). It is a comparison artifact for demos, NOT executed by
-- any tooling in this repo — nothing runs this file automatically.
--
-- COLUMN ORDER IS DELIBERATE AND MUST MATCH THE SOURCE. The S3-staged
-- migration path (`m.add_table_via_s3(...)`, see
-- sources/databricks/prompts/02-migrate-data.md) does
-- `INSERT INTO ... SELECT * FROM s3(...)`, which binds columns positionally,
-- not by name. Reordering a column here — even one that still exists on
-- both sides — silently scrambles data for any table migrated through
-- that path. Column order below matches `samples.tpch.*` /
-- `sources/databricks/scripts/setup_workload.sql`, with augmented columns
-- appended at the end in the order they were added by `ALTER TABLE`.
--
-- `o_orderyear` is the one exception worth flagging: it is `MATERIALIZED`,
-- so ClickHouse excludes it from `SELECT *` and from the implicit column
-- list of a column-less `INSERT INTO ... SELECT * FROM s3(...)` — unlike
-- Databricks' `GENERATED ALWAYS AS`, which is a normal stored column that
-- SELECT * does return. A source unload query that does `SELECT *` will
-- therefore carry one extra column that has no destination slot; the S3
-- unload for `orders` needs an explicit column list that omits
-- `o_orderyear`, not a bare `SELECT *`.
--
-- Source: sources/databricks/scripts/setup_workload.sql (the workload)
--         sources/databricks/queries/sample_olap_queries.sql (the ORDER BY
--         justifications below)
--         librechat/sources/databricks-instructions.md (the type-mapping
--         table this schema follows)

CREATE DATABASE IF NOT EXISTS migration_demo;

-- ── Dimensions ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS migration_demo.region
(
    r_regionkey Int32,
    r_name      LowCardinality(String),
    r_comment   String
)
ENGINE = MergeTree
ORDER BY r_regionkey;

CREATE TABLE IF NOT EXISTS migration_demo.nation
(
    n_nationkey Int32,
    n_name      LowCardinality(String),
    n_regionkey Int32,
    n_comment   String
)
ENGINE = MergeTree
ORDER BY n_nationkey;

-- customer: joined by c_custkey (query 2) and grouped by c_custkey/c_name.
CREATE TABLE IF NOT EXISTS migration_demo.customer
(
    c_custkey    Int64,
    c_name       String,
    c_address    String,
    c_nationkey  Int32,
    c_phone      String,
    c_acctbal    Decimal(15, 2),
    c_mktsegment LowCardinality(String),
    c_comment    String
)
ENGINE = MergeTree
ORDER BY c_custkey;

CREATE TABLE IF NOT EXISTS migration_demo.supplier
(
    s_suppkey   Int64,
    s_name      String,
    s_address   String,
    s_nationkey Int32,
    s_phone     String,
    s_acctbal   Decimal(15, 2),
    s_comment   String
)
ENGINE = MergeTree
ORDER BY s_suppkey;

CREATE TABLE IF NOT EXISTS migration_demo.part
(
    p_partkey     Int64,
    p_name        String,
    p_mfgr        LowCardinality(String),
    p_brand       LowCardinality(String),
    p_type        String,
    p_size        Int32,
    p_container   LowCardinality(String),
    p_retailprice Decimal(15, 2),
    p_comment     String
)
ENGINE = MergeTree
ORDER BY p_partkey;

CREATE TABLE IF NOT EXISTS migration_demo.partsupp
(
    ps_partkey    Int64,
    ps_suppkey    Int64,
    ps_availqty   Int32,
    ps_supplycost Decimal(15, 2),
    ps_comment    String
)
ENGINE = MergeTree
ORDER BY (ps_partkey, ps_suppkey);

-- ── Facts ────────────────────────────────────────────────────────────

-- orders: augmented with o_metadata (VARIANT → JSON) and o_orderyear
-- (GENERATED ALWAYS AS (year(o_orderdate)) on the source → MATERIALIZED
-- here, since the source computes and stores it too, not on every read).
-- ORDER BY starts with o_orderdate: query 5 filters/groups by o_orderyear
-- (derived from it) and the daily_order_summary MV (below) groups by
-- o_orderdate; o_orderpriority as the second key serves both query 2's
-- and query 5's GROUP BY.
CREATE TABLE IF NOT EXISTS migration_demo.orders
(
    o_orderkey      Int64,
    o_custkey       Int64,
    o_orderstatus   LowCardinality(String),
    o_totalprice    Decimal(15, 2),
    o_orderdate     Date32,
    o_orderpriority LowCardinality(String),
    o_clerk         String,
    o_shippriority  Int32,
    o_comment       String,
    o_metadata      JSON,
    o_orderyear     Int32 MATERIALIZED toYear(o_orderdate)
)
ENGINE = MergeTree
ORDER BY (o_orderdate, o_orderpriority);

-- lineitem: augmented with l_shipping_events (ARRAY<STRUCT> → Nested),
-- l_attributes (MAP<STRING,STRING> → Map(String,String)), the
-- TIMESTAMP/TIMESTAMP_NTZ pair, and liquid clustering on
-- (l_shipdate, l_suppkey). ORDER BY keeps the source's clustering intent
-- but is chosen from the actual workload, not copied blindly: query 1
-- filters and groups by l_shipdate; l_orderkey second because it's the
-- join key against orders (query 1) and the FK most other predicates hang
-- off of.
CREATE TABLE IF NOT EXISTS migration_demo.lineitem
(
    l_orderkey         Int64,
    l_partkey          Int64,
    l_suppkey          Int64,
    l_linenumber       Int32,
    l_quantity         Decimal(15, 2),
    l_extendedprice    Decimal(15, 2),
    l_discount         Decimal(15, 2),
    l_tax              Decimal(15, 2),
    l_returnflag       LowCardinality(String),
    l_linestatus       LowCardinality(String),
    l_shipdate         Date32,
    l_commitdate       Date32,
    l_receiptdate      Date32,
    l_shipinstruct     String,
    l_shipmode         LowCardinality(String),
    l_comment          String,
    l_shipping_events Nested
    (
        status   String,
        event_ts DateTime64(6),
        location String
    ),
    l_attributes       Map(String, String),
    l_committed_at     DateTime64(6, 'UTC'),
    l_committed_at_ntz DateTime64(6)
)
ENGINE = MergeTree
ORDER BY (l_shipdate, l_orderkey);

-- ── Materialized aggregate (mirrors the @optional daily_order_summary MV) ──
--
-- The source's daily_order_summary is a Databricks MATERIALIZED VIEW,
-- created only on a serverless SQL warehouse (see setup_workload.sql's
-- `@optional` directive). If it doesn't exist on the source, drop sample
-- query 7 and these two objects together — they are the ClickHouse mirror
-- of that same optional feature, not an independent design choice.
CREATE TABLE IF NOT EXISTS migration_demo.daily_order_summary
(
    order_day       Date32,
    o_orderpriority LowCardinality(String),
    order_count     AggregateFunction(count),
    daily_revenue   AggregateFunction(sum, Decimal(15, 2))
)
ENGINE = AggregatingMergeTree
ORDER BY (order_day, o_orderpriority);

CREATE MATERIALIZED VIEW IF NOT EXISTS migration_demo.daily_order_summary_mv
TO migration_demo.daily_order_summary
AS
SELECT
    o_orderdate                    AS order_day,
    o_orderpriority,
    countState()                   AS order_count,
    sumState(o_totalprice)         AS daily_revenue
FROM migration_demo.orders
GROUP BY o_orderdate, o_orderpriority;
