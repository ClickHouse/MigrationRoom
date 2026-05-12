-- ClickHouse-optimised rewrites of the queries in sample_olap_queries.sql.
-- The agent should produce something equivalent during Phase 5.
-- Differences from the Snowflake originals are commented inline.

-- 1. Daily revenue rollup — runs against the Materialized View backing
--    store via -Merge combinators (replaces the Snowflake Dynamic Table).
SELECT
    order_day,
    o_orderpriority,
    countMerge(order_count_state) AS order_count,
    sumMerge(revenue_state)       AS daily_revenue
FROM migration_target.daily_order_summary_agg
WHERE order_day BETWEEN '1995-01-01' AND '1995-12-31'
GROUP BY order_day, o_orderpriority
ORDER BY order_day, o_orderpriority;

-- 2. Top customers by lifetime revenue — same shape; ClickHouse uses
--    count() instead of COUNT(*) and FINAL is avoided.
SELECT
    c.c_custkey,
    c.c_name,
    n.n_name                                  AS nation,
    count(o.o_orderkey)                       AS order_count,
    sum(o.o_totalprice)                       AS lifetime_revenue
FROM migration_target.orders   o
JOIN migration_target.customer c  ON o.o_custkey = c.c_custkey
JOIN migration_target.nation   n  ON c.c_nationkey = n.n_nationkey
WHERE o.o_orderstatus = 'F'
GROUP BY c.c_custkey, c.c_name, n.n_name
ORDER BY lifetime_revenue DESC
LIMIT 20;

-- 3. Payment method breakdown — JSONExtractString replaces Snowflake's
--    colon-path notation. Optionally cast extracted keys to LowCardinality
--    if you query this frequently.
SELECT
    JSONExtractString(order_metadata, 'payment_method')   AS payment_method,
    JSONExtractString(order_metadata, 'customer_segment') AS customer_segment,
    count()                                               AS order_count,
    sum(o_totalprice)                                     AS revenue
FROM migration_target.orders
WHERE o_orderdate >= '1995-01-01'
  AND o_orderdate <  '1996-01-01'
GROUP BY payment_method, customer_segment
ORDER BY revenue DESC;

-- 4. Delivery latency by ship mode — dateDiff() replaces DATEDIFF;
--    delivery_at is already in UTC so subtraction is straightforward.
SELECT
    l_shipmode,
    count()                                              AS shipment_count,
    avg(dateDiff('hour',
                 toDateTime64(l_shipdate, 3, 'UTC'),
                 delivery_at))                           AS avg_delivery_hours
FROM migration_target.lineitem
WHERE l_shipdate >= '1995-01-01'
  AND l_shipdate <  '1995-04-01'
GROUP BY l_shipmode
ORDER BY shipment_count DESC;

-- 5. Discounted revenue by part — direct rewrite; sum() works on Decimal
--    without explicit casts.
SELECT
    p.p_brand,
    p.p_type,
    sum(l.l_extendedprice * (1 - l.l_discount))  AS net_revenue,
    sum(l.l_quantity)                            AS qty_sold
FROM migration_target.lineitem l
JOIN migration_target.part     p  ON l.l_partkey = p.p_partkey
WHERE l.l_shipdate BETWEEN '1995-01-01' AND '1995-12-31'
GROUP BY p.p_brand, p.p_type
ORDER BY net_revenue DESC
LIMIT 50;

-- ── Function translation cheatsheet ───────────────────────────────────
-- Snowflake                          | ClickHouse
-- COUNT(*)                           | count()
-- DATE_TRUNC('day', col)             | toDate(col)  -- (or toStartOfDay for DateTime)
-- DATEDIFF('hour', a, b)             | dateDiff('hour', a, b)
-- VARIANT col:key::TYPE              | JSONExtract<Type>(col, 'key')
-- CONVERT_TIMEZONE(tz, col)          | toTimeZone(col, tz)
-- NULLIF(x, y)                       | nullIf(x, y)
-- IFNULL / COALESCE                  | ifNull / coalesce
-- QUALIFY ROW_NUMBER() ...           | window + WHERE in outer subquery
