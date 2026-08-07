-- MigrationRoom — Databricks demo workload.
--
-- Builds migration_demo.tpch from the read-only `samples.tpch` catalog that
-- ships with every Databricks workspace (~6M rows, no download), then adds
-- Databricks-specific decoration so the migration agent has to make real
-- decisions rather than a mechanical type-for-type copy.
--
-- Directives read by setup_workload.py:
--   -- @requires: <hint>   abort with <hint> if the next statement fails
--   -- @optional: <hint>   warn and continue if the next statement fails
--
-- Why TPC-H rather than a bespoke workload: see docs/adding-a-source.md.
-- Reusing it lets partners compare Databricks, Snowflake, and BigQuery
-- migrations side by side — same tables, only the decoration differs.

CREATE CATALOG IF NOT EXISTS migration_demo;

CREATE SCHEMA IF NOT EXISTS migration_demo.tpch;

-- ── The 8 TPC-H tables, copied from the built-in samples catalog ──────

CREATE OR REPLACE TABLE migration_demo.tpch.region   AS SELECT * FROM samples.tpch.region;
CREATE OR REPLACE TABLE migration_demo.tpch.nation   AS SELECT * FROM samples.tpch.nation;
CREATE OR REPLACE TABLE migration_demo.tpch.supplier AS SELECT * FROM samples.tpch.supplier;
CREATE OR REPLACE TABLE migration_demo.tpch.customer AS SELECT * FROM samples.tpch.customer;
CREATE OR REPLACE TABLE migration_demo.tpch.part     AS SELECT * FROM samples.tpch.part;
CREATE OR REPLACE TABLE migration_demo.tpch.partsupp AS SELECT * FROM samples.tpch.partsupp;
CREATE OR REPLACE TABLE migration_demo.tpch.orders   AS SELECT * FROM samples.tpch.orders;
CREATE OR REPLACE TABLE migration_demo.tpch.lineitem AS SELECT * FROM samples.tpch.lineitem;

-- ── Augmentation 1: VARIANT on orders ────────────────────────────────
-- Forces a decision: map to ClickHouse JSON, or extract hot keys into
-- typed columns? VARIANT is native from DBSQL 2024.35 / DBR 15.3.

-- @requires: VARIANT requires DBSQL 2024.35+ or DBR 15.3+. Upgrade the SQL warehouse channel to Current, or use a newer runtime.
ALTER TABLE migration_demo.tpch.orders ADD COLUMN o_metadata VARIANT;

UPDATE migration_demo.tpch.orders
SET o_metadata = parse_json(
    concat(
        '{"channel":"',
        element_at(array('web', 'retail', 'partner', 'phone'), cast(pmod(o_orderkey, 4) + 1 AS INT)),
        '","fulfilment":{"warehouse":"WH-',
        cast(pmod(o_orderkey, 7) + 1 AS STRING),
        '","expedited":',
        CASE WHEN o_orderpriority LIKE '1-URGENT%' THEN 'true' ELSE 'false' END,
        '},"discount_codes":["',
        element_at(array('NONE', 'SPRING10', 'LOYALTY5'), cast(pmod(o_orderkey, 3) + 1 AS INT)),
        '"]}'
    )
);

-- ── Augmentation 2: generated column on orders ────────────────────────
-- Forces a decision: MATERIALIZED column or ALIAS column on ClickHouse?

-- @requires: GENERATED ALWAYS AS requires Delta Lake with column generation support (DBR 8.3+).
ALTER TABLE migration_demo.tpch.orders
ADD COLUMN o_orderyear INT GENERATED ALWAYS AS (year(o_orderdate));

-- ── Augmentation 3: nested types on lineitem ──────────────────────────
-- ARRAY<STRUCT> and MAP. Forces a decision between ClickHouse Nested,
-- Array(Tuple(...)), and Map(String, String).

ALTER TABLE migration_demo.tpch.lineitem
ADD COLUMNS (
    l_shipping_events ARRAY<STRUCT<status: STRING, event_ts: TIMESTAMP, location: STRING>>,
    l_attributes      MAP<STRING, STRING>
);

UPDATE migration_demo.tpch.lineitem
SET l_shipping_events = array(
        named_struct(
            'status', 'PACKED',
            'event_ts', cast(l_shipdate AS TIMESTAMP),
            'location', concat('WH-', cast(pmod(l_orderkey, 7) + 1 AS STRING))
        ),
        named_struct(
            'status', CASE
                          WHEN pmod(l_orderkey, 10) = 0 THEN 'CANCELLED'
                          WHEN l_returnflag = 'R'       THEN 'RETURNED'
                          ELSE 'DELIVERED'
                      END,
            'event_ts', cast(l_receiptdate AS TIMESTAMP),
            'location', concat('DC-', cast(pmod(l_partkey, 5) + 1 AS STRING))
        )
    ),
    l_attributes = map(
        'carrier', element_at(array('UPS', 'FEDEX', 'DHL', 'USPS'), cast(pmod(l_orderkey, 4) + 1 AS INT)),
        'fragile', CASE WHEN pmod(l_partkey, 11) = 0 THEN 'true' ELSE 'false' END
    );

-- ── Augmentation 4: TIMESTAMP vs TIMESTAMP_NTZ on lineitem ────────────
-- Forces UTC normalisation and a DateTime64 precision choice.

-- @requires: TIMESTAMP_NTZ requires DBSQL 2023.35+ or DBR 13.3+.
ALTER TABLE migration_demo.tpch.lineitem
ADD COLUMNS (
    l_committed_at     TIMESTAMP,
    l_committed_at_ntz TIMESTAMP_NTZ
);

UPDATE migration_demo.tpch.lineitem
SET l_committed_at     = cast(l_commitdate AS TIMESTAMP),
    l_committed_at_ntz = cast(cast(l_commitdate AS TIMESTAMP) AS TIMESTAMP_NTZ);

-- ── Augmentation 5: liquid clustering on lineitem ─────────────────────
-- Forces a deliberate ClickHouse ORDER BY choice rather than copying a key.

-- @requires: Liquid clustering (CLUSTER BY) requires DBR 13.3+ / DBSQL 2023.40+.
ALTER TABLE migration_demo.tpch.lineitem
CLUSTER BY (l_shipdate, l_suppkey);

-- ── Augmentation 6: deletion vectors + a real history to time-travel ──
-- No ClickHouse equivalent — the agent has to reason about
-- ReplacingMergeTree, ClickPipes, or deferring CDC entirely.

-- @requires: Deletion vectors require DBR 12.2+ / DBSQL 2023.10+.
ALTER TABLE migration_demo.tpch.lineitem
SET TBLPROPERTIES (delta.enableDeletionVectors = true);

DELETE FROM migration_demo.tpch.lineitem
WHERE l_orderkey IN (
    SELECT l_orderkey FROM migration_demo.tpch.lineitem LIMIT 500
);

-- ── Augmentation 7: materialized view (serverless only) ───────────────
-- Recreated as a ClickHouse Materialized View on AggregatingMergeTree.
-- Skipped rather than fatal: materialized views need serverless compute,
-- and a classic warehouse is a perfectly reasonable demo environment.

-- @optional: Materialized views require a serverless SQL warehouse. Skipping — the demo works without it, and sample query 7 should be removed if absent.
CREATE OR REPLACE MATERIALIZED VIEW migration_demo.tpch.daily_order_summary AS
SELECT
    o_orderdate                 AS order_day,
    o_orderpriority,
    count(*)                    AS order_count,
    sum(o_totalprice)           AS daily_revenue
FROM migration_demo.tpch.orders
GROUP BY o_orderdate, o_orderpriority;

-- ── Table comments, so the agent's discovery step has something to read ──

COMMENT ON TABLE migration_demo.tpch.orders IS
    'TPC-H orders, augmented with a VARIANT metadata column and a generated year column.';

COMMENT ON TABLE migration_demo.tpch.lineitem IS
    'TPC-H lineitem, augmented with nested shipping events, a MAP of attributes, TIMESTAMP/TIMESTAMP_NTZ pair, liquid clustering, and deletion vectors.';

-- ── Recompute statistics so DESCRIBE DETAIL reports useful sizes ──────

ANALYZE TABLE migration_demo.tpch.orders   COMPUTE STATISTICS;
ANALYZE TABLE migration_demo.tpch.lineitem COMPUTE STATISTICS;
