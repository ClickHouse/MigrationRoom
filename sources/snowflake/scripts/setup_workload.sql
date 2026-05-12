-- ──────────────────────────────────────────────────────────────────────
-- AI Migration Assistant — Snowflake demo workload
-- ──────────────────────────────────────────────────────────────────────
-- Copies the TPC-H sample tables from SNOWFLAKE_SAMPLE_DATA into a fresh
-- MIGRATION_DEMO.RETAIL schema and adds Snowflake-specific decorations
-- (VARIANT column, TIMESTAMP_TZ column, Clustering Key, Stream, Dynamic
-- Table) so the migration to ClickHouse Cloud is non-trivial.
--
-- Run via:   make snowflake-setup
-- Or:        snowsql -f setup_workload.sql
-- Or:        paste into a Snowsight worksheet
-- ──────────────────────────────────────────────────────────────────────

-- The connection's default warehouse is used. If running this SQL directly
-- via snowsql, set `--warehouse=<your-warehouse>` on the connection.

CREATE DATABASE IF NOT EXISTS MIGRATION_DEMO;
CREATE SCHEMA   IF NOT EXISTS MIGRATION_DEMO.RETAIL;
USE SCHEMA MIGRATION_DEMO.RETAIL;

-- ── 1. Copy TPC-H tables ──────────────────────────────────────────────
-- SNOWFLAKE_SAMPLE_DATA.TPCH_SF1 is shared with every Snowflake account
-- and contains ~6M rows across these 8 tables.
CREATE OR REPLACE TABLE REGION    AS SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.REGION;
CREATE OR REPLACE TABLE NATION    AS SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.NATION;
CREATE OR REPLACE TABLE SUPPLIER  AS SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.SUPPLIER;
CREATE OR REPLACE TABLE PART      AS SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.PART;
CREATE OR REPLACE TABLE PARTSUPP  AS SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.PARTSUPP;
CREATE OR REPLACE TABLE CUSTOMER  AS SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER;
CREATE OR REPLACE TABLE ORDERS    AS SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS;
CREATE OR REPLACE TABLE LINEITEM  AS SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM;

-- ── 2. Augment ORDERS with a semi-structured VARIANT column ───────────
-- Forces the agent to decide how to map VARIANT to ClickHouse (JSON or
-- extracted columns).
ALTER TABLE ORDERS ADD COLUMN ORDER_METADATA VARIANT;
UPDATE ORDERS SET ORDER_METADATA = OBJECT_CONSTRUCT(
    'payment_method',     CASE MOD(O_ORDERKEY, 4) WHEN 0 THEN 'credit_card'
                                                  WHEN 1 THEN 'bank_transfer'
                                                  WHEN 2 THEN 'paypal'
                                                  ELSE        'invoice'
                          END,
    'customer_segment',   CASE MOD(O_ORDERKEY, 3) WHEN 0 THEN 'premium'
                                                  WHEN 1 THEN 'standard'
                                                  ELSE        'economy'
                          END,
    'order_source',       CASE MOD(O_ORDERKEY, 2) WHEN 0 THEN 'web'
                                                  ELSE        'mobile'
                          END,
    'shipping_expedited', MOD(O_ORDERKEY, 5) = 0
);

-- ── 3. Augment LINEITEM with TIMESTAMP_TZ + Clustering Key ────────────
-- TIMESTAMP_TZ forces the agent to handle timezone-aware times correctly.
-- CLUSTER BY forces the agent to translate Snowflake clustering keys to
-- ClickHouse ORDER BY.
ALTER TABLE LINEITEM ADD COLUMN DELIVERY_AT TIMESTAMP_TZ;
UPDATE LINEITEM
   SET DELIVERY_AT = CONVERT_TIMEZONE('America/New_York', 'UTC',
                                      L_SHIPDATE::TIMESTAMP_NTZ);
ALTER TABLE LINEITEM CLUSTER BY (L_ORDERKEY, L_SHIPDATE);

-- ── 4. Stream on ORDERS (CDC — no direct ClickHouse equivalent) ───────
-- Forces the agent to decide: skip / flag / replicate via CH ReplacingMergeTree.
CREATE OR REPLACE STREAM ORDERS_CDC ON TABLE ORDERS;

-- ── 5. Dynamic Table for daily revenue ────────────────────────────────
-- Snowflake's declarative materialization. Translates to a ClickHouse
-- AggregatingMergeTree + Materialized View on the target side.
CREATE OR REPLACE DYNAMIC TABLE DAILY_ORDER_SUMMARY
    TARGET_LAG = '1 hour'
    WAREHOUSE  = COMPUTE_WH
AS
SELECT
    DATE_TRUNC('day', O_ORDERDATE) AS ORDER_DAY,
    O_ORDERPRIORITY,
    COUNT(*)                       AS ORDER_COUNT,
    SUM(O_TOTALPRICE)              AS DAILY_REVENUE
FROM ORDERS
GROUP BY 1, 2;

-- ── Sanity checks ─────────────────────────────────────────────────────
SELECT 'CUSTOMER' AS table_name, COUNT(*) AS row_count FROM CUSTOMER
UNION ALL SELECT 'ORDERS',   COUNT(*) FROM ORDERS
UNION ALL SELECT 'LINEITEM', COUNT(*) FROM LINEITEM
UNION ALL SELECT 'PART',     COUNT(*) FROM PART
UNION ALL SELECT 'SUPPLIER', COUNT(*) FROM SUPPLIER
UNION ALL SELECT 'PARTSUPP', COUNT(*) FROM PARTSUPP
UNION ALL SELECT 'NATION',   COUNT(*) FROM NATION
UNION ALL SELECT 'REGION',   COUNT(*) FROM REGION;

SHOW STREAMS IN SCHEMA MIGRATION_DEMO.RETAIL;
SHOW DYNAMIC TABLES IN SCHEMA MIGRATION_DEMO.RETAIL;

-- Verify the augmentations
SELECT O_ORDERKEY, ORDER_METADATA FROM ORDERS LIMIT 3;
SELECT L_ORDERKEY, L_SHIPDATE, DELIVERY_AT FROM LINEITEM LIMIT 3;
