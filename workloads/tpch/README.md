# Shared TPC-H workload

The TPC-H decision-support benchmark is the workload every cloud-warehouse
migration source in this repo should converge on. Reusing the same 8
tables and the same augmentation contract across sources lets partners
compare migrations side by side — same fact + dimension shape, same
migration challenges shaped to each engine's idioms.

This directory is the standardised loader. BigQuery, Postgres, and
ClickHouse OSS all have loaders under `<source>/`. The Snowflake source
keeps its existing
[`SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`](../../sources/snowflake/scripts/setup_workload.sql)
copy path because Snowflake hosts the data for free — different mechanics,
same end-state. The Databricks source likewise keeps its own
[`setup_workload.sql`](../../sources/databricks/scripts/setup_workload.sql),
downsampling the built-in `samples.tpch` catalog instead of loading from
this directory's `.tbl` files — but it implements the same four-item
augmentation contract in Databricks's idiom (see
[`augmentations.md`](augmentations.md) for the per-engine breakdown).

The Postgres and ClickHouse OSS loaders create a NEW `tpch` database
alongside the bundled e-commerce / web-analytics workloads — partners
switch between demos by toggling `POSTGRES_DB` / `CH_OSS_DB` in `.env`.
The bundled non-TPC-H workloads stay in place.

## The 8 tables (SF1 ≈ 6M rows total)

| Table     | Role      | Approx SF1 rows |
|-----------|-----------|-----------------|
| region    | dimension | 5               |
| nation    | dimension | 25              |
| supplier  | dimension | 10,000          |
| part      | dimension | 200,000         |
| partsupp  | bridge    | 800,000         |
| customer  | dimension | 150,000         |
| orders    | fact      | 1,500,000       |
| lineitem  | fact      | 6,001,215       |

[`schema.sql`](schema.sql) holds the engine-neutral DDL (ANSI SQL with a
PostgreSQL-flavoured type set). Each loader translates that to its own
dialect.

## Augmentation contract

Each loader must apply four post-load decorations, each in the target
engine's idiomatic form, so the migration exercise is non-trivial. See
[`augmentations.md`](augmentations.md) for the full contract — summary:

1. **Semi-structured column on `orders`** — VARIANT / STRUCT / JSON /
   JSONB depending on engine. Forces the agent to decide ClickHouse `JSON`
   vs. extracted typed columns.
2. **Timezone-aware delivery timestamp on `lineitem`** + partition or
   cluster on `l_shipdate` / `l_orderkey`. Forces partition-translation
   and timezone-handling decisions.
3. **Pre-aggregated daily revenue** — engine's native materialisation
   (Dynamic Table / Materialised View / etc.). Forces the agent to design
   a ClickHouse Materialised View on `AggregatingMergeTree` with explicit
   backfill.
4. **Nested array of contact addresses on `customer`** — `ARRAY<STRUCT>`
   or equivalent. Forces ClickHouse `Array(Tuple(...))` translation.

## Running

```bash
# 1. Generate the SF1 .tbl files (idempotent — skipped if data/sf1/ exists).
make tpch-data

# 2. Load + augment into a specific source.
make tpch-load-bigquery
make tpch-load-postgres            # needs `make up` running
make tpch-load-clickhouse-oss      # needs `make up` running
```

Each `make tpch-load-<source>` target reads source-specific env vars (e.g.
`BIGQUERY_PROJECT`, `BIGQUERY_DATASET`) and dispatches the loader under
[`<source>/`](bigquery/).

## Costs

- **Generation**: free; runs locally in a `tpch-dbgen` Docker container.
- **BigQuery load**: ~$0.05 one-time (storage + load job); subsequent
  partner SELECTs are free under BigQuery's 1 TB/mo tier.
- **Storage**: ~1 GB at SF1; ~$0.02/mo on BigQuery.

## Layout

```
workloads/tpch/
├── README.md            this file
├── schema.sql           engine-neutral DDL for the 8 tables
├── augmentations.md     the cross-source augmentation contract
├── dbgen/
│   ├── Dockerfile       builds tpch-dbgen from the official Transaction Processing Performance Council source
│   └── generate.sh      generates SF1 .tbl files into /data/sf1/
├── data/                .tbl output, gitignored
└── bigquery/
    ├── requirements.txt
    ├── load.py              entrypoint: schema → load → augmentations
    └── augmentations.sql    BigQuery-dialect implementation of the contract
```
