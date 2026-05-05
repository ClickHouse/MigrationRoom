-- E-Commerce Analytics Platform — Source Schema (PostgreSQL 16)
-- Designed to exercise common OLAP migration patterns to ClickHouse:
--   JSONB, arrays, ENUMs, TIMESTAMPTZ, BIGSERIAL, partial indexes, window functions

CREATE TYPE order_status AS ENUM (
    'pending', 'confirmed', 'shipped', 'delivered', 'cancelled', 'refunded'
);

-- ── Dimension: User profiles ──────────────────────────────────────────────────
CREATE TABLE users (
    user_id           SERIAL PRIMARY KEY,
    email             VARCHAR(255) UNIQUE NOT NULL,
    username          VARCHAR(100),
    segment           VARCHAR(50),         -- 'high_value','regular','new','churned'
    tags              TEXT[],
    country_code      CHAR(2),
    registration_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lifetime_value    NUMERIC(12,2) DEFAULT 0
);
CREATE INDEX idx_users_country  ON users(country_code);
CREATE INDEX idx_users_segment  ON users(segment);
CREATE INDEX idx_users_reg_date ON users(registration_date);

-- ── Dimension: Product catalog ───────────────────────────────────────────────
CREATE TABLE products (
    product_id  SERIAL PRIMARY KEY,
    sku         VARCHAR(50) UNIQUE NOT NULL,
    name        VARCHAR(255) NOT NULL,
    category    VARCHAR(100),
    subcategory VARCHAR(100),
    price       NUMERIC(10,2) NOT NULL,
    attributes  JSONB DEFAULT '{}',        -- brand, color, weight, etc.
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_products_category ON products(category);
CREATE INDEX idx_products_attrs    ON products USING gin(attributes);

-- ── Sessions ─────────────────────────────────────────────────────────────────
CREATE TABLE sessions (
    session_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          INTEGER REFERENCES users(user_id),
    started_at       TIMESTAMPTZ NOT NULL,
    ended_at         TIMESTAMPTZ,
    page_count       INTEGER DEFAULT 0,
    duration_seconds INTEGER,
    referrer_source  VARCHAR(100),
    device_type      VARCHAR(20),
    country_code     CHAR(2)
);
CREATE INDEX idx_sessions_user       ON sessions(user_id);
CREATE INDEX idx_sessions_started_at ON sessions(started_at);

-- ── Fact: Clickstream events ──────────────────────────────────────────────────
-- Note: session_id is a soft reference to sessions (no FK — for OLAP scale)
--       user_id is a soft reference to users (no FK — for seed performance)
CREATE TABLE events (
    event_id     BIGSERIAL PRIMARY KEY,
    user_id      INTEGER,               -- references users.user_id
    session_id   UUID,                  -- references sessions.session_id
    event_type   VARCHAR(50) NOT NULL,  -- 'page_view','click','add_to_cart','purchase','search'
    page_url     TEXT,
    referrer     TEXT,
    properties   JSONB DEFAULT '{}',
    device_type  VARCHAR(20),
    country_code CHAR(2),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_events_user    ON events(user_id);
CREATE INDEX idx_events_created ON events(created_at);
CREATE INDEX idx_events_type    ON events(event_type);
CREATE INDEX idx_events_session ON events(session_id);
CREATE INDEX idx_events_props   ON events USING gin(properties);

-- ── Fact: Orders ─────────────────────────────────────────────────────────────
CREATE TABLE orders (
    order_id         BIGSERIAL PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(user_id),
    status           order_status NOT NULL DEFAULT 'pending',
    total_amount     NUMERIC(12,2) NOT NULL,
    currency         CHAR(3) DEFAULT 'USD',
    shipping_country CHAR(2),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ
);
CREATE INDEX idx_orders_user    ON orders(user_id);
CREATE INDEX idx_orders_created ON orders(created_at);
CREATE INDEX idx_orders_status  ON orders(status);

-- ── Fact: Order line items ────────────────────────────────────────────────────
CREATE TABLE order_items (
    item_id      BIGSERIAL PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(order_id),
    product_id   INTEGER NOT NULL REFERENCES products(product_id),
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    unit_price   NUMERIC(10,2) NOT NULL,
    discount_pct NUMERIC(5,2) DEFAULT 0
);
CREATE INDEX idx_order_items_order   ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);

-- ── Fact: Ad impressions ──────────────────────────────────────────────────────
-- Note: user_id is a soft reference (no FK — for OLAP scale)
CREATE TABLE ad_impressions (
    impression_id BIGSERIAL PRIMARY KEY,
    campaign_id   INTEGER NOT NULL,
    ad_group      VARCHAR(100),
    creative_id   INTEGER,
    user_id       INTEGER,              -- references users.user_id
    placement     VARCHAR(100),
    cost_micros   BIGINT NOT NULL,
    clicked       BOOLEAN DEFAULT FALSE,
    converted     BOOLEAN DEFAULT FALSE,
    impression_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_impressions_user     ON ad_impressions(user_id);
CREATE INDEX idx_impressions_campaign ON ad_impressions(campaign_id);
CREATE INDEX idx_impressions_at       ON ad_impressions(impression_at);

-- ── Fact: Inventory snapshots (daily) ────────────────────────────────────────
CREATE TABLE inventory_snapshots (
    snapshot_id       BIGSERIAL PRIMARY KEY,
    product_id        INTEGER NOT NULL REFERENCES products(product_id),
    warehouse_code    VARCHAR(20) NOT NULL,
    quantity_on_hand  INTEGER NOT NULL,
    quantity_reserved INTEGER DEFAULT 0,
    snapshot_date     DATE NOT NULL,
    UNIQUE (product_id, warehouse_code, snapshot_date)
);
CREATE INDEX idx_inventory_product ON inventory_snapshots(product_id);
CREATE INDEX idx_inventory_date    ON inventory_snapshots(snapshot_date);
