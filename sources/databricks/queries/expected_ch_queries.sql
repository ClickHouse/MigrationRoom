-- Reference ClickHouse rewrites of sources/databricks/queries/sample_olap_queries.sql.
--
-- Same order, same numbering, one comment per query naming the dialect
-- transformation applied. These run against the schema in
-- expected_ch_schema.sql — that is the point of shipping them alongside it,
-- not a hand-wavy translation exercise. If sample query 7's source
-- (daily_order_summary) doesn't exist because the demo ran on a
-- non-serverless warehouse, drop query 7 here too — see
-- expected_ch_schema.sql's note on that table.

-- 1. Revenue by ship date and priority.
--    Transformation: three-level `catalog.schema.table` → two-level
--    `database.table` naming; otherwise unchanged — this is standard SQL
--    on both engines.
SELECT
    l_shipdate,
    o_orderpriority,
    count(*)                                AS line_count,
    sum(l_extendedprice * (1 - l_discount)) AS revenue
FROM migration_demo.lineitem l
JOIN migration_demo.orders   o ON l.l_orderkey = o.o_orderkey
WHERE l_shipdate BETWEEN '1994-01-01' AND '1994-12-31'
GROUP BY l_shipdate, o_orderpriority
ORDER BY l_shipdate, o_orderpriority;

-- 2. Top customers by lifetime revenue.
--    Transformation: three-level → two-level naming only; the multi-table
--    join and LIMIT are identical on both engines.
SELECT
    c.c_custkey,
    c.c_name,
    n.n_name                    AS nation,
    count(o.o_orderkey)         AS order_count,
    sum(o.o_totalprice)         AS lifetime_revenue
FROM migration_demo.orders   o
JOIN migration_demo.customer c ON o.o_custkey = c.c_custkey
JOIN migration_demo.nation   n ON c.c_nationkey = n.n_nationkey
GROUP BY c.c_custkey, c.c_name, n.n_name
ORDER BY lifetime_revenue DESC
LIMIT 50;

-- 3. VARIANT extraction.
--    Transformation: `o_metadata:channel::string` and
--    `o_metadata:fulfilment.warehouse::string` (Databricks colon-path syntax
--    over VARIANT) → `JSONExtractString(o_metadata, 'channel')` and a
--    two-key `JSONExtractString(o_metadata, 'fulfilment', 'warehouse')`
--    (nested-path drill-down) over the `JSON` column.
SELECT
    JSONExtractString(o_metadata, 'channel')                    AS channel,
    JSONExtractString(o_metadata, 'fulfilment', 'warehouse')    AS warehouse,
    count(*)                                                    AS order_count,
    sum(o_totalprice)                                           AS revenue
FROM migration_demo.orders
WHERE o_orderdate >= '1995-01-01'
GROUP BY channel, warehouse
ORDER BY revenue DESC;

-- 4. Nested-type access.
--    Transformation: `LATERAL VIEW explode(l_shipping_events) AS event` over
--    an ARRAY<STRUCT> → `ARRAY JOIN` over the parallel arrays ClickHouse's
--    `Nested` type produces (`l_shipping_events.status`,
--    `l_shipping_events.location`); the MAP subscript
--    `l_attributes['carrier']` is unchanged — same syntax on both engines.
SELECT
    event_status                        AS status,
    l.l_shipmode                        AS ship_mode,
    l.l_attributes['carrier']           AS carrier,
    count(*)                            AS event_count
FROM migration_demo.lineitem AS l
ARRAY JOIN l.l_shipping_events.status AS event_status
GROUP BY event_status, l.l_shipmode, l.l_attributes['carrier']
ORDER BY event_count DESC;

-- 5. Window function with QUALIFY.
--    Transformation: `QUALIFY revenue_rank <= 3` (ClickHouse has no
--    QUALIFY) → the window predicate moves into an outer `WHERE` over a
--    subquery. Also reads `o_orderyear`, the `MATERIALIZED` mirror of the
--    source's `GENERATED ALWAYS AS` column.
SELECT *
FROM
(
    SELECT
        o_orderyear,
        o_orderpriority,
        sum(o_totalprice)                   AS revenue,
        rank() OVER (
            PARTITION BY o_orderyear
            ORDER BY sum(o_totalprice) DESC
        )                                    AS revenue_rank
    FROM migration_demo.orders
    GROUP BY o_orderyear, o_orderpriority
)
WHERE revenue_rank <= 3
ORDER BY o_orderyear, revenue_rank;

-- 6. Higher-order function over an array.
--    Transformation: `filter(arr, e -> pred)` → `arrayFilter(e -> pred, arr)`
--    (argument order flips); `aggregate(arr, 0, (acc, e) -> acc + 1)` (a
--    manual count-fold) → `arrayReduce('count', ...)` over the filtered
--    array — ClickHouse has a named aggregate combinator for this instead
--    of a manual fold.
SELECT
    l_shipmode,
    count(*)                                                            AS line_count,
    avg(
        arrayReduce('count', arrayFilter(x -> x != 'CANCELLED', l_shipping_events.status))
    )                                                                    AS avg_live_events
FROM migration_demo.lineitem
GROUP BY l_shipmode
ORDER BY line_count DESC;

-- 7. Pre-aggregated read against the materialized view.
--    Transformation: the source reads a Databricks MATERIALIZED VIEW
--    directly as finished rows. Its ClickHouse mirror is an
--    AggregatingMergeTree holding *partial aggregate states*
--    (see expected_ch_schema.sql), so reading it back requires the
--    `-Merge` combinator (`countMerge`, `sumMerge`) to finalize
--    `order_count` / `daily_revenue` — a plain SELECT would return opaque
--    state blobs, not numbers. Drop this query if daily_order_summary
--    doesn't exist because the source ran without a serverless warehouse.
SELECT
    order_day,
    o_orderpriority,
    countMerge(order_count)   AS order_count,
    sumMerge(daily_revenue)   AS daily_revenue
FROM migration_demo.daily_order_summary
WHERE order_day BETWEEN '1995-01-01' AND '1995-12-31'
GROUP BY order_day, o_orderpriority
ORDER BY order_day, o_orderpriority;
