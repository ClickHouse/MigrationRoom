-- Representative OLAP queries for the migration exercise
-- Partners migrate these from Postgres → ClickHouse syntax in Phase 5.

-- Q1: Simple aggregation — daily revenue by country (last 30 days)
SELECT
    DATE_TRUNC('day', created_at)  AS day,
    shipping_country,
    COUNT(*)                        AS order_count,
    SUM(total_amount)               AS revenue
FROM orders
WHERE created_at >= NOW() - INTERVAL '30 days'
  AND status IN ('delivered', 'shipped')
GROUP BY 1, 2
ORDER BY 1 DESC, revenue DESC;

-- Q2: Window function — running total of revenue per user
SELECT
    user_id,
    order_id,
    created_at,
    total_amount,
    SUM(total_amount) OVER (PARTITION BY user_id ORDER BY created_at) AS running_total
FROM orders
WHERE status != 'cancelled'
ORDER BY user_id, created_at;

-- Q3: CTE funnel — page view → cart → purchase conversion rates
WITH funnel AS (
    SELECT
        DATE_TRUNC('day', created_at)                                    AS day,
        COUNT(*) FILTER (WHERE event_type = 'page_view')                 AS views,
        COUNT(*) FILTER (WHERE event_type = 'add_to_cart')               AS carts,
        COUNT(*) FILTER (WHERE event_type = 'purchase')                  AS purchases
    FROM events
    WHERE created_at >= NOW() - INTERVAL '7 days'
    GROUP BY 1
)
SELECT
    day, views, carts, purchases,
    ROUND(carts::NUMERIC     / NULLIF(views, 0) * 100, 2) AS view_to_cart_pct,
    ROUND(purchases::NUMERIC / NULLIF(carts, 0) * 100, 2) AS cart_to_purchase_pct
FROM funnel
ORDER BY day DESC;

-- Q4: JSONB query — filter events by nested JSON property
SELECT event_type, COUNT(*) AS cnt
FROM events
WHERE  properties->>'label' LIKE 'item_%'
  AND (properties->>'value')::NUMERIC > 50
GROUP BY event_type
ORDER BY cnt DESC;

-- Q5: Multi-table JOIN — order details with product categories and user segments
SELECT
    u.segment,
    p.category,
    COUNT(DISTINCT o.order_id)                           AS orders,
    SUM(oi.quantity * oi.unit_price)::NUMERIC(14,2)      AS gross_revenue
FROM orders o
JOIN users       u  ON u.user_id    = o.user_id
JOIN order_items oi ON oi.order_id  = o.order_id
JOIN products    p  ON p.product_id = oi.product_id
WHERE o.status = 'delivered'
  AND o.created_at >= NOW() - INTERVAL '90 days'
GROUP BY 1, 2
ORDER BY gross_revenue DESC;

-- Q6: Time-series rollup — hourly event counts with fill for missing hours
WITH hours AS (
    SELECT generate_series(
        DATE_TRUNC('hour', NOW() - INTERVAL '24 hours'),
        DATE_TRUNC('hour', NOW()),
        INTERVAL '1 hour'
    ) AS hour
),
counts AS (
    SELECT DATE_TRUNC('hour', created_at) AS hour, COUNT(*) AS cnt
    FROM events
    WHERE created_at >= NOW() - INTERVAL '24 hours'
    GROUP BY 1
)
SELECT h.hour, COALESCE(c.cnt, 0) AS event_count
FROM hours h
LEFT JOIN counts c ON c.hour = h.hour
ORDER BY h.hour;

-- Q7: Cohort analysis — user retention by registration month
WITH cohorts AS (
    SELECT user_id,
           DATE_TRUNC('month', registration_date) AS cohort_month
    FROM users
),
activity AS (
    SELECT e.user_id,
           DATE_TRUNC('month', e.created_at) AS activity_month
    FROM events e
    GROUP BY 1, 2
)
SELECT
    c.cohort_month,
    EXTRACT(MONTH FROM AGE(a.activity_month, c.cohort_month))::INT AS months_since_signup,
    COUNT(DISTINCT a.user_id)                                       AS retained_users
FROM cohorts c
JOIN activity a ON a.user_id = c.user_id AND a.activity_month >= c.cohort_month
GROUP BY 1, 2
ORDER BY 1, 2;

-- Q8: Top-N per group — top 5 products per category by revenue
WITH ranked AS (
    SELECT
        p.category,
        p.name,
        SUM(oi.quantity * oi.unit_price) AS revenue,
        RANK() OVER (
            PARTITION BY p.category
            ORDER BY SUM(oi.quantity * oi.unit_price) DESC
        ) AS rk
    FROM order_items oi
    JOIN products p ON p.product_id = oi.product_id
    GROUP BY p.category, p.name
)
SELECT category, name, revenue::NUMERIC(14,2), rk
FROM ranked
WHERE rk <= 5
ORDER BY category, rk;

-- Q9: Session analysis — p50/p90/p99 duration by referrer, with percentiles
SELECT
    referrer_source,
    COUNT(*)                                                           AS sessions,
    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY duration_seconds)) AS p50_sec,
    ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY duration_seconds)) AS p90_sec,
    ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY duration_seconds)) AS p99_sec
FROM sessions
WHERE duration_seconds IS NOT NULL
GROUP BY 1
ORDER BY sessions DESC;

-- Q10: Ad attribution — CTR and conversion rate by campaign and placement
SELECT
    campaign_id,
    placement,
    COUNT(*)                                                        AS impressions,
    SUM(clicked::INT)                                               AS clicks,
    SUM(converted::INT)                                             AS conversions,
    ROUND(SUM(clicked::INT)::NUMERIC   / COUNT(*) * 100, 3)        AS ctr_pct,
    ROUND(SUM(converted::INT)::NUMERIC / NULLIF(SUM(clicked::INT), 0) * 100, 3) AS cvr_pct,
    ROUND(SUM(cost_micros) / 1e6, 2)                               AS total_cost_usd
FROM ad_impressions
WHERE impression_at >= NOW() - INTERVAL '30 days'
GROUP BY 1, 2
ORDER BY impressions DESC
LIMIT 50;
