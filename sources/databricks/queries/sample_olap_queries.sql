-- Sample OLAP queries for the migration_demo.tpch workload.
-- Classic TPC-H analytical patterns plus queries that hit the
-- Databricks-specific augmentations (VARIANT, STRUCT/ARRAY/MAP,
-- liquid clustering, generated column, materialized view).
--
-- The dashboard substitutes this file into the step-1 prompt, where the
-- agent uses it to choose ORDER BY keys, partitioning, and codecs.

-- 1. Revenue by ship date and priority — the bread-and-butter rollup.
--    Drives the ORDER BY choice on the migrated lineitem table.
SELECT
    l_shipdate,
    o_orderpriority,
    count(*)                                   AS line_count,
    sum(l_extendedprice * (1 - l_discount))    AS revenue
FROM migration_demo.tpch.lineitem l
JOIN migration_demo.tpch.orders   o ON l.l_orderkey = o.o_orderkey
WHERE l_shipdate BETWEEN DATE '1994-01-01' AND DATE '1994-12-31'
GROUP BY l_shipdate, o_orderpriority
ORDER BY l_shipdate, o_orderpriority;

-- 2. Top customers by lifetime revenue — multi-table join where the
--    GROUP BY columns live in dimensions, not the fact table.
SELECT
    c.c_custkey,
    c.c_name,
    n.n_name                    AS nation,
    count(o.o_orderkey)         AS order_count,
    sum(o.o_totalprice)         AS lifetime_revenue
FROM migration_demo.tpch.orders   o
JOIN migration_demo.tpch.customer c ON o.o_custkey = c.c_custkey
JOIN migration_demo.tpch.nation   n ON c.c_nationkey = n.n_nationkey
GROUP BY c.c_custkey, c.c_name, n.n_name
ORDER BY lifetime_revenue DESC
LIMIT 50;

-- 3. VARIANT extraction — reads the o_metadata column with Databricks'
--    colon path syntax. On ClickHouse this becomes JSONExtract* over a
--    JSON column, or a typed column if the agent extracted hot keys.
SELECT
    o_metadata:channel::string           AS channel,
    o_metadata:fulfilment.warehouse::string AS warehouse,
    count(*)                             AS order_count,
    sum(o_totalprice)                    AS revenue
FROM migration_demo.tpch.orders
WHERE o_orderdate >= DATE '1995-01-01'
GROUP BY channel, warehouse
ORDER BY revenue DESC;

-- 4. Nested-type access — explodes the ARRAY<STRUCT> shipping events and
--    reads the MAP. `explode` becomes arrayJoin on ClickHouse; the MAP
--    subscript becomes a Map(String, String) lookup.
SELECT
    event.status                        AS status,
    l.l_shipmode                        AS ship_mode,
    l.l_attributes['carrier']           AS carrier,
    count(*)                            AS event_count
FROM migration_demo.tpch.lineitem l
LATERAL VIEW explode(l.l_shipping_events) AS event
GROUP BY event.status, l.l_shipmode, l.l_attributes['carrier']
ORDER BY event_count DESC;

-- 5. Window function with QUALIFY — Databricks supports QUALIFY, ClickHouse
--    does not, so this must be rewritten as a subquery with WHERE.
--    Also reads o_orderyear, the GENERATED ALWAYS AS column.
SELECT
    o_orderyear,
    o_orderpriority,
    sum(o_totalprice)                   AS revenue,
    rank() OVER (
        PARTITION BY o_orderyear
        ORDER BY sum(o_totalprice) DESC
    )                                   AS revenue_rank
FROM migration_demo.tpch.orders
GROUP BY o_orderyear, o_orderpriority
QUALIFY revenue_rank <= 3
ORDER BY o_orderyear, revenue_rank;

-- 6. Higher-order function — `aggregate` and `filter` over an array have no
--    direct ClickHouse syntax; they map to arrayReduce / arrayFilter.
SELECT
    l_shipmode,
    count(*)                            AS line_count,
    avg(
        aggregate(
            filter(l_shipping_events, e -> e.status <> 'CANCELLED'),
            0,
            (acc, e) -> acc + 1
        )
    )                                   AS avg_live_events
FROM migration_demo.tpch.lineitem
GROUP BY l_shipmode
ORDER BY line_count DESC;

-- 7. Pre-aggregated read against the materialized view (skipped by the
--    setup script on non-serverless warehouses — if daily_order_summary
--    does not exist, drop this query).
SELECT
    order_day,
    o_orderpriority,
    order_count,
    daily_revenue
FROM migration_demo.tpch.daily_order_summary
WHERE order_day BETWEEN DATE '1995-01-01' AND DATE '1995-12-31'
ORDER BY order_day, o_orderpriority;
