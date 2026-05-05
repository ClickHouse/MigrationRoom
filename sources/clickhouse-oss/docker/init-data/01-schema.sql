-- Web Analytics Platform — ClickHouse OSS Schema
-- Domain: multi-tenant SaaS analytics (tracks pageviews, sessions, conversions)

CREATE DATABASE IF NOT EXISTS analytics;

-- ── Dimension Tables ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS analytics.projects
(
    project_id   UInt32,
    name         String,
    domain       String,
    timezone     String,
    plan         LowCardinality(String),   -- free | starter | pro | enterprise
    created_at   DateTime
)
ENGINE = MergeTree()
ORDER BY project_id;

-- ── Fact Tables ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS analytics.sessions
(
    session_id       String,
    project_id       UInt32,
    visitor_id       UInt64,
    started_at       DateTime,
    duration_seconds UInt32,
    pageview_count   UInt16,
    is_bounce        UInt8,
    entry_page       String,
    exit_page        String,
    referrer_domain  LowCardinality(String),
    utm_source       LowCardinality(String),
    utm_medium       LowCardinality(String),
    utm_campaign     String,
    device_type      LowCardinality(String),  -- desktop | mobile | tablet
    browser          LowCardinality(String),
    os               LowCardinality(String),
    country_code     LowCardinality(String),
    city             String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(started_at)
ORDER BY (project_id, visitor_id, started_at)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS analytics.pageviews
(
    timestamp       DateTime,
    project_id      UInt32,
    visitor_id      UInt64,
    session_id      String,
    url             String,
    referrer        String,
    duration_seconds UInt16,
    scroll_depth    UInt8,                   -- 0–100
    properties      Map(String, String),
    device_type     LowCardinality(String),
    country_code    LowCardinality(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (project_id, timestamp, visitor_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS analytics.conversions
(
    timestamp    DateTime,
    project_id   UInt32,
    visitor_id   UInt64,
    session_id   String,
    goal_name    LowCardinality(String),
    revenue      Decimal(10, 2),
    properties   Map(String, String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (project_id, goal_name, timestamp)
SETTINGS index_granularity = 8192;

-- ── Pre-aggregated Table (AggregatingMergeTree) ──────────────────────────────

CREATE TABLE IF NOT EXISTS analytics.daily_stats
(
    date             Date,
    project_id       UInt32,
    referrer_domain  LowCardinality(String),
    device_type      LowCardinality(String),
    country_code     LowCardinality(String),
    visitors         AggregateFunction(uniq, UInt64),
    sessions         SimpleAggregateFunction(sum, UInt64),
    pageviews        SimpleAggregateFunction(sum, UInt64),
    bounces          SimpleAggregateFunction(sum, UInt64),
    total_duration   SimpleAggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, project_id, referrer_domain, device_type, country_code);

-- ── Materialized View → populates daily_stats from sessions ──────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.mv_daily_stats
TO analytics.daily_stats
AS
SELECT
    toDate(started_at)             AS date,
    project_id,
    referrer_domain,
    device_type,
    country_code,
    uniqState(visitor_id)          AS visitors,
    count()                        AS sessions,
    sum(pageview_count)            AS pageviews,
    sum(is_bounce)                 AS bounces,
    sum(duration_seconds)          AS total_duration
FROM analytics.sessions
GROUP BY date, project_id, referrer_domain, device_type, country_code;
