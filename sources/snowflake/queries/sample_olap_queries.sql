-- Sample OLAP queries for the MIGRATION_DEMO.RETAIL workload.
-- Mix of classic TPC-H analytical patterns plus queries that hit the
-- Snowflake-specific augmentations (VARIANT, TIMESTAMP_TZ, Dynamic Table).
--
-- Paste these into the migration agent during Phase 2 (analyse queries) to
-- extract ORDER BY key and partition recommendations.

-- 1. Daily revenue rollup — hits the Dynamic Table directly.
--    On the ClickHouse side this becomes a Materialized View on
--    AggregatingMergeTree (or a query against the underlying ORDERS table).
SELECT
    ORDER_DAY,
    O_ORDERPRIORITY,
    ORDER_COUNT,
    DAILY_REVENUE
FROM MIGRATION_DEMO.RETAIL.DAILY_ORDER_SUMMARY
WHERE ORDER_DAY BETWEEN '1995-01-01' AND '1995-12-31'
ORDER BY ORDER_DAY, O_ORDERPRIORITY;

-- 2. Top customers by lifetime revenue — multi-table join.
--    Exercises the agent's ability to reason about ORDER BY when the
--    primary GROUP BY column (C_NAME / C_CUSTKEY) lives in a dimension.
SELECT
    C.C_CUSTKEY,
    C.C_NAME,
    N.N_NAME                                AS NATION,
    COUNT(O.O_ORDERKEY)                     AS ORDER_COUNT,
    SUM(O.O_TOTALPRICE)                     AS LIFETIME_REVENUE
FROM MIGRATION_DEMO.RETAIL.ORDERS    O
JOIN MIGRATION_DEMO.RETAIL.CUSTOMER  C  ON O.O_CUSTKEY = C.C_CUSTKEY
JOIN MIGRATION_DEMO.RETAIL.NATION    N  ON C.C_NATIONKEY = N.N_NATIONKEY
WHERE O.O_ORDERSTATUS = 'F'
GROUP BY C.C_CUSTKEY, C.C_NAME, N.N_NAME
ORDER BY LIFETIME_REVENUE DESC
LIMIT 20;

-- 3. Payment method breakdown — extracts a key from the VARIANT column.
--    Forces the agent to choose between (a) extracting hot keys into typed
--    columns on the ClickHouse side, or (b) keeping a JSON column and using
--    JSONExtractString() at query time.
SELECT
    ORDER_METADATA:payment_method::VARCHAR       AS payment_method,
    ORDER_METADATA:customer_segment::VARCHAR     AS customer_segment,
    COUNT(*)                                     AS order_count,
    SUM(O_TOTALPRICE)                            AS revenue
FROM MIGRATION_DEMO.RETAIL.ORDERS
WHERE O_ORDERDATE >= '1995-01-01'
  AND O_ORDERDATE <  '1996-01-01'
GROUP BY payment_method, customer_segment
ORDER BY revenue DESC;

-- 4. Delivery latency by ship mode — uses the augmented TIMESTAMP_TZ column.
--    Forces the agent to think about timezone normalisation (TIMESTAMP_TZ →
--    DateTime64(3, 'UTC')) on the ClickHouse side.
SELECT
    L_SHIPMODE,
    COUNT(*)                                     AS shipment_count,
    AVG(DATEDIFF('hour',
                 L_SHIPDATE::TIMESTAMP_NTZ,
                 DELIVERY_AT::TIMESTAMP_NTZ))    AS avg_delivery_hours
FROM MIGRATION_DEMO.RETAIL.LINEITEM
WHERE L_SHIPDATE >= '1995-01-01'
  AND L_SHIPDATE <  '1995-04-01'
GROUP BY L_SHIPMODE
ORDER BY shipment_count DESC;

-- 5. Discounted revenue by part — classic TPC-H Q1 / Q3 pattern.
--    Heavy aggregation; benefits from a Materialized View on the CH side.
SELECT
    P.P_BRAND,
    P.P_TYPE,
    SUM(L.L_EXTENDEDPRICE * (1 - L.L_DISCOUNT))  AS net_revenue,
    SUM(L.L_QUANTITY)                            AS qty_sold
FROM MIGRATION_DEMO.RETAIL.LINEITEM L
JOIN MIGRATION_DEMO.RETAIL.PART     P  ON L.L_PARTKEY = P.P_PARTKEY
WHERE L.L_SHIPDATE BETWEEN '1995-01-01' AND '1995-12-31'
GROUP BY P.P_BRAND, P.P_TYPE
ORDER BY net_revenue DESC
LIMIT 50;
