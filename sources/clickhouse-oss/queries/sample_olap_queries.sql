-- Web Analytics OLAP Queries — ClickHouse OSS
-- Schema: analytics.{projects, sessions, pageviews, conversions, daily_stats}
-- Run these against the local ClickHouse OSS instance (http://localhost:8123)
-- The migration exercise rewrites them for ClickHouse Cloud.

-- ── Q1: Daily Traffic Summary (pre-aggregated) ───────────────────────────────
-- Uses the AggregatingMergeTree daily_stats table.
-- uniqMerge() reconstructs the HyperLogLog sketch stored by uniqState().
SELECT
    date,
    uniqMerge(visitors)                                         AS unique_visitors,
    sum(sessions)                                               AS sessions,
    sum(pageviews)                                              AS pageviews,
    round(sum(bounces) / sum(sessions) * 100, 1)               AS bounce_rate_pct,
    round(sum(total_duration) / sum(sessions), 0)              AS avg_session_duration_sec
FROM analytics.daily_stats
WHERE project_id = 1
  AND date BETWEEN '2024-01-01' AND '2024-12-31'
GROUP BY date
ORDER BY date;

-- ── Q2: Top Pages by Engagement Score ────────────────────────────────────────
-- Composite score: avg scroll depth × avg time on page / 100
-- Only surfaces URLs with meaningful traffic (≥ 100 pageviews).
SELECT
    url,
    count()                                                     AS pageviews,
    round(avg(scroll_depth), 1)                                 AS avg_scroll_depth,
    round(avg(duration_seconds), 0)                             AS avg_time_on_page_sec,
    round(avg(scroll_depth) * avg(duration_seconds) / 100, 1)  AS engagement_score
FROM analytics.pageviews
WHERE project_id = 1
  AND timestamp >= '2024-01-01'
GROUP BY url
HAVING pageviews >= 100
ORDER BY engagement_score DESC
LIMIT 20;

-- ── Q3: Traffic Source Attribution ───────────────────────────────────────────
-- Sessions by referrer domain + UTM medium with quality metrics.
-- Empty strings map to readable labels for dashboards.
SELECT
    if(referrer_domain = '', '(direct)', referrer_domain)       AS source,
    if(utm_medium = '', '(none)', utm_medium)                   AS medium,
    count()                                                     AS sessions,
    uniq(visitor_id)                                            AS unique_visitors,
    round(sum(is_bounce) / count() * 100, 1)                   AS bounce_rate_pct,
    round(avg(duration_seconds), 0)                             AS avg_session_sec,
    round(avg(pageview_count), 1)                               AS avg_pages_per_session
FROM analytics.sessions
WHERE project_id = 1
  AND started_at >= '2024-01-01'
GROUP BY source, medium
ORDER BY sessions DESC
LIMIT 25;

-- ── Q4: Device & Browser Matrix ──────────────────────────────────────────────
-- Cross-tab of device type vs browser — informs front-end optimisation priority.
SELECT
    device_type,
    browser,
    count()                                                     AS sessions,
    round(sum(is_bounce) / count() * 100, 1)                   AS bounce_rate_pct,
    round(avg(duration_seconds), 0)                             AS avg_duration_sec
FROM analytics.sessions
WHERE project_id = 1
  AND started_at >= toStartOfMonth(today())
GROUP BY device_type, browser
ORDER BY sessions DESC;

-- ── Q5: Conversion Goal Funnel with Revenue ───────────────────────────────────
-- Aggregates counts and revenue by goal type.
-- avg_order_value only averages over paid conversions (revenue > 0).
SELECT
    goal_name,
    count()                                                     AS conversions,
    countIf(revenue > 0)                                        AS paid_conversions,
    round(sum(revenue), 2)                                      AS total_revenue,
    round(avg(if(revenue > 0, toFloat64(revenue), NULL)), 2)    AS avg_order_value,
    uniq(visitor_id)                                            AS unique_converters
FROM analytics.conversions
WHERE project_id = 1
  AND timestamp >= '2024-01-01'
GROUP BY goal_name
ORDER BY conversions DESC;

-- ── Q6: Geographic Traffic Distribution ──────────────────────────────────────
-- Visitors and conversion rate by country.
-- LEFT JOIN ensures sessions without conversions are still counted.
SELECT
    s.country_code,
    uniq(s.visitor_id)                                          AS unique_visitors,
    count()                                                     AS sessions,
    round(sum(s.is_bounce) / count() * 100, 1)                 AS bounce_rate_pct,
    countIf(c.goal_name != '')                                  AS conversions,
    round(countIf(c.goal_name != '') / count() * 100, 2)       AS conversion_rate_pct
FROM analytics.sessions AS s
LEFT JOIN analytics.conversions AS c
    ON s.session_id = c.session_id AND s.project_id = c.project_id
WHERE s.project_id = 1
  AND s.started_at >= '2024-01-01'
GROUP BY s.country_code
ORDER BY unique_visitors DESC
LIMIT 30;

-- ── Q7: A/B Test Variant Analysis ────────────────────────────────────────────
-- Compares engagement metrics by A/B variant stored in Map(String, String) properties.
-- Map access: properties['key'] returns '' when key is absent (not NULL).
SELECT
    properties['ab_variant']                                    AS variant,
    count()                                                     AS pageviews,
    round(avg(scroll_depth), 1)                                 AS avg_scroll_depth,
    round(avg(duration_seconds), 0)                             AS avg_time_sec,
    round(countIf(scroll_depth >= 75) / count() * 100, 1)      AS deep_read_rate_pct,
    round(countIf(properties['logged_in'] = 'true') / count() * 100, 1) AS logged_in_pct
FROM analytics.pageviews
WHERE project_id = 1
  AND url = '/features'
  AND timestamp >= '2024-01-01'
GROUP BY variant
ORDER BY variant;

-- ── Q8: New vs Returning Visitor Classification ──────────────────────────────
-- Tags each session as 'new' (first ever session) or 'returning'.
-- Uses a CTE to find each visitor's first session timestamp.
WITH first_sessions AS (
    SELECT
        visitor_id,
        min(started_at) AS first_session_at
    FROM analytics.sessions
    WHERE project_id = 1
    GROUP BY visitor_id
)
SELECT
    if(s.started_at = f.first_session_at, 'new', 'returning')  AS visitor_type,
    count()                                                     AS sessions,
    round(sum(s.is_bounce) / count() * 100, 1)                 AS bounce_rate_pct,
    round(avg(s.duration_seconds), 0)                           AS avg_duration_sec,
    round(avg(s.pageview_count), 1)                             AS avg_pages
FROM analytics.sessions AS s
INNER JOIN first_sessions AS f ON s.visitor_id = f.visitor_id
WHERE s.project_id = 1
  AND s.started_at >= '2024-01-01'
GROUP BY visitor_type
ORDER BY visitor_type;

-- ── Q9: UTM Campaign Performance Dashboard ────────────────────────────────────
-- Pairs sessions with downstream conversions to compute per-campaign ROI.
-- revenue_per_session is the primary efficiency metric for paid campaigns.
SELECT
    if(s.utm_campaign = '', '(no campaign)', s.utm_campaign)        AS campaign,
    if(s.utm_source = '', '(direct)', s.utm_source)                 AS source,
    count(DISTINCT s.session_id)                                     AS sessions,
    uniq(s.visitor_id)                                               AS reach,
    count(c.session_id)                                              AS conversions,
    round(count(c.session_id) / count(DISTINCT s.session_id) * 100, 2) AS cvr_pct,
    round(sum(c.revenue), 2)                                         AS total_revenue,
    round(sum(c.revenue) / nullIf(count(DISTINCT s.session_id), 0), 4) AS revenue_per_session
FROM analytics.sessions AS s
LEFT JOIN analytics.conversions AS c
    ON s.session_id = c.session_id AND s.project_id = c.project_id
WHERE s.project_id = 1
  AND s.started_at >= '2024-01-01'
  AND s.utm_source != ''
GROUP BY campaign, source
ORDER BY total_revenue DESC
LIMIT 20;

-- ── Q10: Entry → Exit Page Flow Matrix ───────────────────────────────────────
-- Most common entry/exit page pairs — reveals where visitors drop off.
-- Window function computes each exit page's share within its entry page cohort.
SELECT
    entry_page,
    exit_page,
    count()                                                             AS sessions,
    round(
        count() * 100.0 / sum(count()) OVER (PARTITION BY entry_page),
        1
    )                                                                   AS exit_pct_from_entry,
    round(avg(duration_seconds), 0)                                     AS avg_session_sec,
    round(sum(is_bounce) / count() * 100, 1)                           AS bounce_rate_pct
FROM analytics.sessions
WHERE project_id = 1
  AND started_at >= '2024-01-01'
GROUP BY entry_page, exit_page
HAVING sessions >= 50
ORDER BY entry_page, sessions DESC;
