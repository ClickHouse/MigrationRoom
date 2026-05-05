-- Seed realistic web analytics data
-- Data range: 2024-01-01 to 2025-12-31 (2 years)
-- Volumes: 1K projects, 2M sessions, 10M pageviews, 200K conversions

-- ── Projects (1,000 rows) ─────────────────────────────────────────────────────

INSERT INTO analytics.projects
SELECT
    number + 1                                                                AS project_id,
    concat('Project ', toString(number + 1))                                  AS name,
    concat('site-', toString(number + 1), '.example.com')                     AS domain,
    arrayElement(['UTC','America/New_York','Europe/London','Asia/Tokyo','Australia/Sydney'],
        (number % 5) + 1)                                                     AS timezone,
    arrayElement(['free','free','free','starter','starter','pro','enterprise'],
        (number % 7) + 1)                                                     AS plan,
    toDateTime('2023-01-01') + toIntervalDay(number % 365)                    AS created_at
FROM numbers(1000);

-- ── Sessions (2,000,000 rows) ─────────────────────────────────────────────────

INSERT INTO analytics.sessions
SELECT
    lower(hex(sipHash64(number)))                                             AS session_id,
    (sipHash64(number)     % 1000) + 1                                        AS project_id,
    (sipHash64(number * 3) % 200000) + 1                                      AS visitor_id,
    toDateTime('2024-01-01') + toIntervalSecond(
        sipHash64(number * 7) % (365 * 24 * 3600 * 2))                       AS started_at,
    (sipHash64(number * 11) % 1800) + 10                                      AS duration_seconds,
    (sipHash64(number * 13) % 15) + 1                                         AS pageview_count,
    if(sipHash64(number * 17) % 4 = 0, 1, 0)                                 AS is_bounce,
    arrayElement(['/','/','/dashboard','/features','/pricing','/blog','/docs','/signup'],
        (sipHash64(number * 19) % 8) + 1)                                     AS entry_page,
    arrayElement(['/','/','/dashboard','/features','/pricing','/blog','/docs','/contact'],
        (sipHash64(number * 23) % 8) + 1)                                     AS exit_page,
    arrayElement(['','','google.com','facebook.com','twitter.com','linkedin.com',
        'github.com','bing.com','duckduckgo.com','newsletter'],
        (sipHash64(number * 29) % 10) + 1)                                    AS referrer_domain,
    arrayElement(['','','','organic','cpc','email','social','referral'],
        (sipHash64(number * 31) % 8) + 1)                                     AS utm_source,
    arrayElement(['','organic','cpc','email','social'],
        (sipHash64(number * 37) % 5) + 1)                                     AS utm_medium,
    if(sipHash64(number * 41) % 5 = 0,
        arrayElement(['summer-promo','product-launch','newsletter-jan','retargeting-q1'],
            (sipHash64(number * 43) % 4) + 1),
        '')                                                                   AS utm_campaign,
    arrayElement(['desktop','desktop','desktop','mobile','mobile','tablet'],
        (sipHash64(number * 47) % 6) + 1)                                     AS device_type,
    arrayElement(['Chrome','Chrome','Firefox','Safari','Safari','Edge','Opera'],
        (sipHash64(number * 53) % 7) + 1)                                     AS browser,
    arrayElement(['Windows','Windows','macOS','macOS','Linux','iOS','Android'],
        (sipHash64(number * 59) % 7) + 1)                                     AS os,
    arrayElement(['US','US','US','GB','DE','FR','CA','AU','JP','IN','BR','NL'],
        (sipHash64(number * 61) % 12) + 1)                                    AS country_code,
    arrayElement(['New York','London','Berlin','Paris','Toronto','Sydney',
        'Tokyo','Mumbai','São Paulo','Amsterdam','','',''],
        (sipHash64(number * 67) % 13) + 1)                                    AS city
FROM numbers(2000000);

-- ── Pageviews (10,000,000 rows) ───────────────────────────────────────────────

INSERT INTO analytics.pageviews
SELECT
    toDateTime('2024-01-01') + toIntervalSecond(
        sipHash64(number * 71) % (365 * 24 * 3600 * 2))                      AS timestamp,
    (sipHash64(number * 73) % 1000) + 1                                       AS project_id,
    (sipHash64(number * 79) % 200000) + 1                                     AS visitor_id,
    lower(hex(sipHash64(number * 83)))                                        AS session_id,
    arrayElement(['/','/dashboard','/analytics','/features','/pricing',
        '/blog','/docs','/signup','/login','/settings','/reports','/integrations'],
        (sipHash64(number * 89) % 12) + 1)                                    AS url,
    if(sipHash64(number * 97) % 3 = 0,
        concat('https://',
            arrayElement(['google.com','twitter.com','github.com','linkedin.com',''],
                (sipHash64(number * 101) % 5) + 1)),
        '')                                                                   AS referrer,
    (sipHash64(number * 103) % 300) + 5                                       AS duration_seconds,
    (sipHash64(number * 107) % 101)                                           AS scroll_depth,
    map(
        'ab_variant',   arrayElement(['A','B','C'], (sipHash64(number * 109) % 3) + 1),
        'logged_in',    if(sipHash64(number * 113) % 2 = 0, 'true', 'false'),
        'plan',         arrayElement(['free','starter','pro','enterprise'],
                            (sipHash64(number * 127) % 4) + 1)
    )                                                                         AS properties,
    arrayElement(['desktop','desktop','desktop','mobile','mobile','tablet'],
        (sipHash64(number * 131) % 6) + 1)                                   AS device_type,
    arrayElement(['US','US','US','GB','DE','FR','CA','AU','JP','IN','BR','NL'],
        (sipHash64(number * 137) % 12) + 1)                                  AS country_code
FROM numbers(10000000);

-- ── Conversions (200,000 rows) ────────────────────────────────────────────────

INSERT INTO analytics.conversions
SELECT
    toDateTime('2024-01-01') + toIntervalSecond(
        sipHash64(number * 139) % (365 * 24 * 3600 * 2))                     AS timestamp,
    (sipHash64(number * 149) % 1000) + 1                                      AS project_id,
    (sipHash64(number * 151) % 200000) + 1                                    AS visitor_id,
    lower(hex(sipHash64(number * 157)))                                       AS session_id,
    arrayElement(['signup','signup','trial_start','upgrade_to_pro',
        'upgrade_to_enterprise','purchase','demo_request','contact_sales'],
        (sipHash64(number * 163) % 8) + 1)                                   AS goal_name,
    if(sipHash64(number * 167) % 3 = 0,
        toDecimal64((sipHash64(number * 173) % 50000) / 100.0, 2),
        toDecimal64(0, 2))                                                    AS revenue,
    map(
        'plan',         arrayElement(['starter','pro','enterprise'],
                            (sipHash64(number * 179) % 3) + 1),
        'coupon',       if(sipHash64(number * 181) % 5 = 0, 'SAVE20', ''),
        'trial_days',   toString(sipHash64(number * 191) % 15 + 7)
    )                                                                         AS properties
FROM numbers(200000);

-- ── Backfill daily_stats from existing sessions ───────────────────────────────
-- (The MV only captures future inserts; existing data must be backfilled manually)

INSERT INTO analytics.daily_stats
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
