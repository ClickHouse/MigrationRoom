-- Deterministic data generation for the E-Commerce Analytics Platform
-- Scale: small (~1M events, ~1 min) / medium (~10M, ~5-10 min) / large (~30M, ~20-30 min)
-- Controlled by app.dataset_size GUC set by 00-configure.sh
-- Fixed setseed(0.42) ensures reproducible results

DO $$
DECLARE
    v_size          TEXT := COALESCE(current_setting('app.dataset_size', true), 'medium');
    v_n_users       INT;
    v_n_products    INT;
    v_n_sessions    INT;
    v_n_events      INT;
    v_n_orders      INT;
    v_n_impressions INT;
BEGIN
    -- Scale factors
    IF v_size = 'small' THEN
        v_n_users := 20000;  v_n_products := 5000;   v_n_sessions := 300000;
        v_n_events := 1000000; v_n_orders := 50000;  v_n_impressions := 500000;
    ELSIF v_size = 'large' THEN
        v_n_users := 600000; v_n_products := 150000; v_n_sessions := 9000000;
        v_n_events := 30000000; v_n_orders := 1500000; v_n_impressions := 15000000;
    ELSE
        v_n_users := 200000; v_n_products := 50000;  v_n_sessions := 3000000;
        v_n_events := 10000000; v_n_orders := 500000; v_n_impressions := 5000000;
    END IF;

    RAISE NOTICE 'Seeding dataset=% | users=% products=% sessions=% events=% orders=% impressions=%',
        v_size, v_n_users, v_n_products, v_n_sessions, v_n_events, v_n_orders, v_n_impressions;
    PERFORM setseed(0.42);

    -- ── Users ─────────────────────────────────────────────────────────────────
    RAISE NOTICE '[1/7] Seeding users...';
    INSERT INTO users (email, username, segment, tags, country_code, registration_date, lifetime_value)
    SELECT
        'user' || i || '@example.com',
        'user_' || i,
        (ARRAY['high_value','regular','new','churned'])[1 + (floor(random()*4))::INT],
        ARRAY['tag_' || (1+floor(random()*10)::INT), 'tag_' || (1+floor(random()*10)::INT)],
        (ARRAY['US','GB','DE','JP','SG','AU','CA','FR','IN','BR'])[1+(floor(random()*10))::INT],
        NOW() - (floor(random()*730))::INT * INTERVAL '1 day',
        round((random()*5000)::NUMERIC, 2)
    FROM generate_series(1, v_n_users) AS gs(i);

    -- ── Products ─────────────────────────────────────────────────────────────
    RAISE NOTICE '[2/7] Seeding products...';
    INSERT INTO products (sku, name, category, subcategory, price, attributes, created_at)
    SELECT
        'SKU-' || LPAD(i::TEXT, 8, '0'),
        'Product ' || i,
        (ARRAY['Electronics','Clothing','Home','Sports','Books','Beauty','Food','Toys'])[1+(floor(random()*8))::INT],
        'Sub_' || (1+floor(random()*5)::INT),
        round((1 + random()*999)::NUMERIC, 2),
        jsonb_build_object(
            'brand',     'Brand_' || (1+floor(random()*20)::INT),
            'color',     (ARRAY['red','blue','green','black','white'])[1+(floor(random()*5))::INT],
            'weight_kg', round((random()*10)::NUMERIC, 2)
        ),
        NOW() - (floor(random()*365))::INT * INTERVAL '1 day'
    FROM generate_series(1, v_n_products) AS gs(i);

    -- ── Sessions ──────────────────────────────────────────────────────────────
    RAISE NOTICE '[3/7] Seeding sessions...';
    INSERT INTO sessions (user_id, started_at, ended_at, page_count, duration_seconds,
                          referrer_source, device_type, country_code)
    SELECT
        1 + (floor(random()*(v_n_users-1)))::INT,
        NOW() - (floor(random()*365))::INT * INTERVAL '1 day'
             - (floor(random()*86400))::INT * INTERVAL '1 second',
        NOW() - (floor(random()*30))::INT  * INTERVAL '1 day',
        1 + (floor(random()*30))::INT,
        (floor(random()*3600))::INT,
        (ARRAY['google','direct','email','facebook','twitter','affiliate','none'])[1+(floor(random()*7))::INT],
        (ARRAY['desktop','mobile','tablet'])[1+(floor(random()*3))::INT],
        (ARRAY['US','GB','DE','JP','SG','AU','CA','FR','IN','BR'])[1+(floor(random()*10))::INT]
    FROM generate_series(1, v_n_sessions) AS gs(i);

    -- ── Session ID lookup table (for fast events seeding) ─────────────────────
    -- Sampling 50K sessions avoids per-row correlated subquery O(n) cost
    CREATE TEMP TABLE _session_sample AS
        SELECT session_id, row_number() OVER () AS n
        FROM sessions
        LIMIT 50000;
    CREATE INDEX ON _session_sample(n);
    ANALYZE _session_sample;

    -- ── Events ────────────────────────────────────────────────────────────────
    RAISE NOTICE '[4/7] Seeding events (largest table — may take several minutes)...';
    INSERT INTO events (user_id, session_id, event_type, page_url, properties,
                        device_type, country_code, created_at)
    SELECT
        1 + (floor(random()*(v_n_users-1)))::INT,
        ss.session_id,
        (ARRAY['page_view','click','add_to_cart','remove_from_cart','purchase','search'])[1+(floor(random()*6))::INT],
        '/page/' || (1+floor(random()*100)::INT),
        jsonb_build_object(
            'value', round((random()*100)::NUMERIC, 2),
            'label', 'item_' || (1+floor(random()*50)::INT)
        ),
        (ARRAY['desktop','mobile','tablet'])[1+(floor(random()*3))::INT],
        (ARRAY['US','GB','DE','JP','SG','AU','CA','FR','IN','BR'])[1+(floor(random()*10))::INT],
        NOW() - (floor(random()*365))::INT * INTERVAL '1 day'
             - (floor(random()*86400))::INT * INTERVAL '1 second'
    FROM generate_series(1, v_n_events) AS gs(i)
    JOIN _session_sample ss ON ss.n = (((gs.i - 1) % 50000) + 1);

    DROP TABLE _session_sample;

    -- ── Orders ────────────────────────────────────────────────────────────────
    RAISE NOTICE '[5/7] Seeding orders...';
    INSERT INTO orders (user_id, status, total_amount, currency, shipping_country,
                        created_at, updated_at)
    SELECT
        1 + (floor(random()*(v_n_users-1)))::INT,
        (ARRAY['pending','confirmed','shipped','delivered','cancelled','refunded']::order_status[])[1+(floor(random()*6))::INT],
        round((10 + random()*990)::NUMERIC, 2),
        (ARRAY['USD','EUR','GBP','JPY','SGD'])[1+(floor(random()*5))::INT],
        (ARRAY['US','GB','DE','JP','SG','AU','CA','FR','IN','BR'])[1+(floor(random()*10))::INT],
        NOW() - (floor(random()*365))::INT * INTERVAL '1 day',
        NOW() - (floor(random()*30))::INT  * INTERVAL '1 day'
    FROM generate_series(1, v_n_orders) AS gs(i);

    -- ── Order items (2-4 items per order, set-based) ──────────────────────────
    RAISE NOTICE '[6/7] Seeding order items...';
    INSERT INTO order_items (order_id, product_id, quantity, unit_price, discount_pct)
    SELECT
        o.order_id,
        1 + (floor(random()*(v_n_products-1)))::INT,
        1 + (floor(random()*4))::INT,
        round((1 + random()*499)::NUMERIC, 2),
        round((random()*30)::NUMERIC, 2)
    FROM orders o
    CROSS JOIN generate_series(1, 2 + (floor(random()*3))::INT) AS gs(j);

    -- ── Ad impressions ────────────────────────────────────────────────────────
    RAISE NOTICE '[7/7] Seeding ad impressions...';
    INSERT INTO ad_impressions (campaign_id, ad_group, creative_id, user_id, placement,
                                cost_micros, clicked, converted, impression_at)
    SELECT
        1 + (floor(random()*100))::INT,
        'group_' || (1+floor(random()*10)::INT),
        1 + (floor(random()*500))::INT,
        1 + (floor(random()*(v_n_users-1)))::INT,
        (ARRAY['homepage','search','product','checkout','sidebar'])[1+(floor(random()*5))::INT],
        1000 + (floor(random()*9000))::BIGINT,
        random() < 0.05,
        random() < 0.01,
        NOW() - (floor(random()*365))::INT * INTERVAL '1 day'
             - (floor(random()*86400))::INT * INTERVAL '1 second'
    FROM generate_series(1, v_n_impressions) AS gs(i);

    -- ── Inventory snapshots (last 7 days × all products × 5 warehouses) ───────
    RAISE NOTICE '[+] Seeding inventory snapshots...';
    INSERT INTO inventory_snapshots (product_id, warehouse_code, quantity_on_hand,
                                     quantity_reserved, snapshot_date)
    SELECT
        p.product_id,
        w.wh,
        (floor(random()*1000))::INT,
        (floor(random()*50))::INT,
        d.dt
    FROM products p
    CROSS JOIN (VALUES ('US-EAST'),('US-WEST'),('EU-DE'),('AP-SG'),('AP-AU')) AS w(wh)
    CROSS JOIN (
        SELECT (NOW() - s * INTERVAL '1 day')::DATE AS dt
        FROM generate_series(0, 6) AS gs(s)
    ) d
    ON CONFLICT (product_id, warehouse_code, snapshot_date) DO NOTHING;

    -- ── Post-seed consistency updates ─────────────────────────────────────────
    RAISE NOTICE 'Updating order totals and user lifetime values...';

    UPDATE orders o
    SET total_amount = sub.total
    FROM (
        SELECT order_id,
               SUM(quantity * unit_price * (1 - discount_pct/100.0)) AS total
        FROM order_items GROUP BY order_id
    ) sub
    WHERE o.order_id = sub.order_id;

    UPDATE users u
    SET lifetime_value = sub.ltv
    FROM (
        SELECT user_id, COALESCE(SUM(total_amount), 0) AS ltv
        FROM orders WHERE status = 'delivered' GROUP BY user_id
    ) sub
    WHERE u.user_id = sub.user_id;

    RAISE NOTICE 'Seed complete. Running ANALYZE...';
END $$;

ANALYZE;
