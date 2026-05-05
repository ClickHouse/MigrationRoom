-- Reference ClickHouse rewrites of the 10 sample OLAP queries
-- Answer key — compare against AI-assisted migration results

-- Q1: Daily revenue by country (ClickHouse)
/*
SELECT
    toStartOfDay(created_at) AS day,
    shipping_country,
    count()          AS order_count,
    sum(total_amount) AS revenue
FROM migration_target.orders
WHERE created_at >= now() - INTERVAL 30 DAY
  AND status IN ('delivered', 'shipped')
GROUP BY day, shipping_country
ORDER BY day DESC, revenue DESC;
*/

-- Q2: Running total per user (ClickHouse window function)
/*
SELECT
    user_id,
    order_id,
    created_at,
    total_amount,
    sum(total_amount) OVER (PARTITION BY user_id ORDER BY created_at) AS running_total
FROM migration_target.orders
WHERE status != 'cancelled'
ORDER BY user_id, created_at;
*/

-- Q3: Funnel analysis (ClickHouse countIf — no CTE needed)
/*
SELECT
    toStartOfDay(created_at)                                              AS day,
    countIf(event_type = 'page_view')                                     AS views,
    countIf(event_type = 'add_to_cart')                                   AS carts,
    countIf(event_type = 'purchase')                                      AS purchases,
    round(countIf(event_type = 'add_to_cart')
          / nullIf(countIf(event_type = 'page_view'), 0) * 100, 2)       AS view_to_cart_pct,
    round(countIf(event_type = 'purchase')
          / nullIf(countIf(event_type = 'add_to_cart'), 0) * 100, 2)     AS cart_to_purchase_pct
FROM migration_target.events
WHERE created_at >= now() - INTERVAL 7 DAY
GROUP BY day
ORDER BY day DESC;
*/

-- Q4: JSON property filter (ClickHouse JSON functions)
/*
SELECT event_type, count() AS cnt
FROM migration_target.events
WHERE JSONExtractString(properties, 'label') LIKE 'item_%'
  AND JSONExtractFloat(properties, 'value') > 50
GROUP BY event_type
ORDER BY cnt DESC;
*/

-- Q5: Multi-table JOIN (larger table on left)
/*
SELECT
    u.segment,
    p.category,
    uniq(o.order_id)                          AS orders,
    round(sum(oi.quantity * oi.unit_price), 2) AS gross_revenue
FROM migration_target.order_items oi
JOIN migration_target.orders   o ON o.order_id  = oi.order_id
JOIN migration_target.users    u ON u.user_id   = o.user_id
JOIN migration_target.products p ON p.product_id = oi.product_id
WHERE o.status = 'delivered'
  AND o.created_at >= now() - INTERVAL 90 DAY
GROUP BY u.segment, p.category
ORDER BY gross_revenue DESC;
*/

-- Q6: Time-series with gap fill (ClickHouse WITH FILL)
/*
SELECT
    toStartOfHour(created_at) AS hour,
    count()                    AS event_count
FROM migration_target.events
WHERE created_at >= now() - INTERVAL 24 HOUR
GROUP BY hour
ORDER BY hour
WITH FILL
    FROM toStartOfHour(now() - INTERVAL 24 HOUR)
    TO   toStartOfHour(now())
    STEP INTERVAL 1 HOUR;
*/

-- Q7: Cohort retention (ClickHouse)
/*
SELECT
    toStartOfMonth(u.registration_date)                           AS cohort_month,
    dateDiff('month', u.registration_date, e.created_at)         AS months_since_signup,
    uniq(e.user_id)                                               AS retained_users
FROM migration_target.events e
JOIN migration_target.users u ON u.user_id = e.user_id
WHERE dateDiff('month', u.registration_date, e.created_at) >= 0
GROUP BY cohort_month, months_since_signup
ORDER BY cohort_month, months_since_signup;
*/

-- Q8: Top-5 products per category (ClickHouse)
/*
SELECT
    p.category,
    p.name,
    round(sum(oi.quantity * oi.unit_price), 2) AS revenue,
    row_number() OVER (PARTITION BY p.category ORDER BY sum(oi.quantity * oi.unit_price) DESC) AS rk
FROM migration_target.order_items oi
JOIN migration_target.products p ON p.product_id = oi.product_id
GROUP BY p.category, p.name
HAVING rk <= 5
ORDER BY p.category, rk;
*/

-- Q9: Session duration percentiles (ClickHouse quantile functions)
/*
SELECT
    referrer_source,
    count()                             AS sessions,
    round(quantile(0.50)(duration_seconds)) AS p50_sec,
    round(quantile(0.90)(duration_seconds)) AS p90_sec,
    round(quantile(0.99)(duration_seconds)) AS p99_sec
FROM migration_target.sessions
WHERE duration_seconds > 0
GROUP BY referrer_source
ORDER BY sessions DESC;
*/

-- Q10: Ad attribution CTR/CVR (ClickHouse)
/*
SELECT
    campaign_id,
    placement,
    count()                                                    AS impressions,
    countIf(clicked)                                           AS clicks,
    countIf(converted)                                         AS conversions,
    round(countIf(clicked)    / count() * 100, 3)             AS ctr_pct,
    round(countIf(converted)  / nullIf(countIf(clicked), 0) * 100, 3) AS cvr_pct,
    round(sum(cost_micros) / 1e6, 2)                          AS total_cost_usd
FROM migration_target.ad_impressions
WHERE impression_at >= now() - INTERVAL 30 DAY
GROUP BY campaign_id, placement
ORDER BY impressions DESC
LIMIT 50;
*/
