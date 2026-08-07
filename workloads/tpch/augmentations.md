# Augmentation contract

TPC-H out of the box is a pure relational schema. To exercise the
migration agent on engine-specific features that don't translate cleanly
to ClickHouse, every loader applies the four augmentations below, each in
the target engine's idiomatic form.

The agent's job at migration time is to inspect the source schema —
including these augmentations — and decide how to project them onto
ClickHouse. There is no single "correct" mapping; the report should
document *why* each decision was made.

---

## 1. Semi-structured column on `orders`

Add an `order_metadata` column carrying these four keys per order:

| Key                 | Type    | Generation rule (deterministic from `o_orderkey`) |
|---------------------|---------|---------------------------------------------------|
| `payment_method`    | string  | `o_orderkey % 4 = 0 → credit_card; 1 → bank_transfer; 2 → paypal; else invoice` |
| `customer_segment`  | string  | `o_orderkey % 3 = 0 → premium; 1 → standard; else economy` |
| `order_source`      | string  | `o_orderkey % 2 = 0 → web; else mobile` |
| `shipping_expedited`| boolean | `o_orderkey % 5 = 0` |

| Engine        | Native type           |
|---------------|-----------------------|
| Snowflake     | `VARIANT` (populated via `OBJECT_CONSTRUCT`) |
| BigQuery      | `STRUCT<...>`         |
| Databricks    | `VARIANT` (populated via `parse_json`) |
| PostgreSQL    | `JSONB`               |
| ClickHouse OSS| `Map(String, String)` or `JSON` |

**Migration decision:** ClickHouse `JSON` (keep semi-structured) vs.
flatten to typed columns. Both are defensible — depends on query patterns.

---

## 2. Timezone-aware delivery timestamp on `lineitem`

Add a `delivery_at` column to `lineitem` derived from `l_shipdate`,
converted to a timezone-aware timestamp.

- Source semantic: a timestamp in `America/New_York`, equal to `l_shipdate
  00:00:00 EST` (i.e. midnight local time), persisted in the engine's
  timezone-aware type.
- The loader is responsible for the conversion and for documenting which
  timezone semantics the engine actually persists (e.g. Snowflake
  `TIMESTAMP_TZ` stores the original timezone offset; BigQuery `TIMESTAMP`
  stores UTC and is timezone-naive at the type level but the load tag
  identifies the source TZ).

Also: cluster or partition `lineitem` on `l_shipdate` and order on
`l_orderkey`.

| Engine        | Implementation |
|---------------|----------------|
| Snowflake     | `delivery_at TIMESTAMP_TZ` + `CLUSTER BY (l_orderkey, l_shipdate)` |
| BigQuery      | `delivery_at TIMESTAMP` + `PARTITION BY DATE(l_shipdate)` + `CLUSTER BY l_orderkey` |
| Databricks    | `l_committed_at TIMESTAMP` (UTC-normalized) + `l_committed_at_ntz TIMESTAMP_NTZ` (naive) pair + liquid clustering `CLUSTER BY (l_shipdate, l_suppkey)` |
| PostgreSQL    | `delivery_at TIMESTAMPTZ` + BRIN index on `l_shipdate` |
| ClickHouse OSS| `delivery_at DateTime64(3, 'America/New_York')` + `ORDER BY (l_shipdate, l_orderkey)` |

**Migration decision:** ClickHouse target type — `DateTime64(3, 'UTC')`
with conversion at the source, vs. `DateTime64(3, 'America/New_York')`.
Partition translation — Snowflake's CLUSTER BY / BigQuery's PARTITION BY
both become `ORDER BY` + `PARTITION BY` on the target.

---

## 3. Pre-aggregated daily revenue

Materialise a daily revenue rollup over `orders`, grouped by
`(order_day, o_orderpriority)`, with `count(*)` and `sum(o_totalprice)`.

| Engine        | Native materialisation |
|---------------|------------------------|
| Snowflake     | `DYNAMIC TABLE daily_order_summary` with `TARGET_LAG = '1 hour'` |
| BigQuery      | `MATERIALIZED VIEW daily_order_summary` (auto-refresh on commit) |
| Databricks    | `MATERIALIZED VIEW daily_order_summary` (serverless SQL warehouse only — skipped, not fatal, on a classic warehouse) |
| PostgreSQL    | `MATERIALIZED VIEW daily_order_summary` (manual `REFRESH`) |
| ClickHouse OSS| Pre-existing `MATERIALIZED VIEW mv_daily_stats` on `AggregatingMergeTree` |

**Migration decision:** ClickHouse Materialised View on
`AggregatingMergeTree` with `countState()` / `sumState()` aggregates +
mandatory backfill INSERT.

---

## 4. Nested array of contact addresses on `customer`

Add a `contact_addresses` column to `customer` holding 1–3 addresses per
customer (e.g. billing, shipping, work).

Per-row generation rule (deterministic from `c_custkey`):
- `c_custkey % 3 + 1` addresses
- Each address: `{ line: '<random>', city: '<random>', country: '<2-letter>' }`

| Engine        | Native type |
|---------------|-------------|
| Snowflake     | `ARRAY` of `OBJECT` (via `VARIANT`) |
| BigQuery      | `ARRAY<STRUCT<line STRING, city STRING, country STRING>>` |
| Databricks    | `ARRAY<STRUCT<status STRING, event_ts TIMESTAMP, location STRING>>` + a sibling `MAP<STRING, STRING>` — placed on `lineitem` (`l_shipping_events` / `l_attributes`), not `customer`; Databricks's demo shapes this augmentation as shipping events rather than contact addresses |
| PostgreSQL    | `JSONB` array of objects |
| ClickHouse OSS| `Array(Tuple(line String, city String, country String))` |

**Migration decision:** ClickHouse `Array(Tuple(...))` (typed, fast) vs.
`Array(JSON)` (lazy, flexible), plus (for Databricks) a `Map(String,
String)` vs. `Map(LowCardinality(String), String)` call for the sibling
MAP column. Snowflake has no clean `ARRAY<STRUCT>` equivalent — partners
migrating from Snowflake may not encounter this; that's fine. BigQuery,
Databricks, and Postgres partners will.
