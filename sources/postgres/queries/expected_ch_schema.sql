-- Reference ClickHouse schema — answer key for the migration exercise
-- Partners compare their AI-assisted results against this baseline.
-- Hidden from the agent by default; reveal for self-assessment.

-- ── Dimension: Users ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_target.users
(
    user_id           UInt32,
    email             String,
    username          String,
    segment           LowCardinality(String),
    tags              Array(String),
    country_code      LowCardinality(FixedString(2)),
    registration_date DateTime64(3, 'UTC'),
    lifetime_value    Decimal(12, 2)
)
ENGINE = ReplacingMergeTree(registration_date)
ORDER BY (segment, country_code, user_id)
PARTITION BY toYYYYMM(registration_date)
SETTINGS index_granularity = 8192;

-- ── Dimension: Products ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_target.products
(
    product_id  UInt32,
    sku         String,
    name        String,
    category    LowCardinality(String),
    subcategory LowCardinality(String),
    price       Decimal(10, 2),
    attributes  JSON,
    created_at  DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (category, subcategory, product_id);

-- ── Sessions ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_target.sessions
(
    session_id       UUID,
    user_id          UInt32,
    started_at       DateTime64(3, 'UTC'),
    ended_at         DateTime64(3, 'UTC'),
    page_count       UInt16,
    duration_seconds UInt32,
    referrer_source  LowCardinality(String),
    device_type      LowCardinality(String),
    country_code     LowCardinality(FixedString(2))
)
ENGINE = MergeTree
ORDER BY (started_at, user_id)
PARTITION BY toYYYYMM(started_at);

-- ── Fact: Events (largest table — primary analytical workload) ───────────────
-- ORDER BY designed for the most common access patterns:
--   - filter by event_type (lowest cardinality → first)
--   - filter by country (low cardinality)
--   - range scan on time
--   - filter/join on user_id
CREATE TABLE IF NOT EXISTS migration_target.events
(
    event_id     UInt64,
    user_id      UInt32,
    session_id   UUID,
    event_type   LowCardinality(String),
    page_url     String,
    referrer     String,
    properties   JSON,
    device_type  LowCardinality(String),
    country_code LowCardinality(FixedString(2)),
    created_at   DateTime64(3, 'UTC')
)
ENGINE = MergeTree
ORDER BY (event_type, country_code, created_at, user_id)
PARTITION BY toYYYYMM(created_at);

-- ── Fact: Orders ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_target.orders
(
    order_id         UInt64,
    user_id          UInt32,
    status           LowCardinality(String),
    total_amount     Decimal(12, 2),
    currency         LowCardinality(FixedString(3)),
    shipping_country LowCardinality(FixedString(2)),
    created_at       DateTime64(3, 'UTC'),
    updated_at       DateTime64(3, 'UTC')
)
ENGINE = MergeTree
ORDER BY (status, created_at, user_id)
PARTITION BY toYYYYMM(created_at);

-- ── Fact: Order items ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_target.order_items
(
    item_id      UInt64,
    order_id     UInt64,
    product_id   UInt32,
    quantity     UInt8,
    unit_price   Decimal(10, 2),
    discount_pct Decimal(5, 2)
)
ENGINE = MergeTree
ORDER BY (order_id, product_id);

-- ── Fact: Ad impressions ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_target.ad_impressions
(
    impression_id UInt64,
    campaign_id   UInt32,
    ad_group      LowCardinality(String),
    creative_id   UInt32,
    user_id       UInt32,
    placement     LowCardinality(String),
    cost_micros   Int64,
    clicked       Bool,
    converted     Bool,
    impression_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
ORDER BY (campaign_id, placement, impression_at)
PARTITION BY toYYYYMM(impression_at);

-- ── Fact: Inventory snapshots ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS migration_target.inventory_snapshots
(
    snapshot_id       UInt64,
    product_id        UInt32,
    warehouse_code    LowCardinality(String),
    quantity_on_hand  Int32,
    quantity_reserved Int32,
    snapshot_date     Date
)
ENGINE = ReplacingMergeTree(snapshot_date)
ORDER BY (product_id, warehouse_code, snapshot_date)
PARTITION BY toYYYYMM(snapshot_date);
