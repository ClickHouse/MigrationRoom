"""
MigrationRoom — TPC-H workload loader for ClickHouse OSS.

Loads the TPC-H SF1 .tbl files produced by `make tpch-data` into a
dedicated `tpch` database on the target ClickHouse OSS instance and
applies the cross-source augmentation contract (see
../augmentations.md) in ClickHouse-native form:

    1. orders.order_metadata Map(String, String)         (semi-structured)
    2. lineitem.delivery_at DateTime64(3, 'America/New_York') + ORDER BY
    3. mv_daily_stats MATERIALIZED VIEW on AggregatingMergeTree
    4. customer.contact_addresses Array(Tuple(...))

Coexistence strategy: writes to a NEW `tpch` database alongside the
bundled `analytics` web-analytics workload. The existing partner-facing
demo (sources/clickhouse-oss/docker/init-data) is untouched. Partners
point the migration agent at TPC-H by setting `CH_OSS_DB=tpch` in their
`.env`.

Note: in CH OSS the TPC-H tables themselves use MergeTree, so the
"pre-existing AggregatingMergeTree" augmentation from augmentations.md
takes the form of a Materialized View on top of `orders` (not a base
table conversion) — that's the closest engine-native realization of
"pre-aggregated rollup that's already optimised in the source".

Usage:
    pip install -r workloads/tpch/clickhouse-oss/requirements.txt
    set -a; source .env; set +a
    python3 workloads/tpch/clickhouse-oss/load.py

Environment:
    CH_OSS_HOST       default: localhost  (use 'clickhouse-oss' inside docker)
    CH_OSS_PORT       default: 8123
    CH_OSS_USER       default: default
    CH_OSS_PASSWORD   default: (empty)
    CH_OSS_DB         default: tpch       (the DB this script creates)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import clickhouse_connect

WORKLOAD_DIR = Path(__file__).resolve().parents[1]   # workloads/tpch/
DATA_DIR = WORKLOAD_DIR / "data" / "sf1"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"  # CH-specific DDL
AUGMENT_SQL = Path(__file__).resolve().parent / "augmentations.sql"

TPCH_TABLES = [
    "region", "nation", "supplier", "part",
    "partsupp", "customer", "orders", "lineitem",
]

# Expected SF1 row counts — post-load sanity check.
SF1_EXPECTED_ROWS = {
    "region": 5,
    "nation": 25,
    "supplier": 10_000,
    "part": 200_000,
    "partsupp": 800_000,
    "customer": 150_000,
    "orders": 1_500_000,
    "lineitem": 6_001_215,
}

# ClickHouse-native schema. ClickHouse has no engine-neutral CREATE
# TABLE — every table needs an ENGINE clause + ORDER BY. We keep this
# file local rather than translating the engine-neutral schema.sql at
# runtime, which would be more code for no win.
CH_SCHEMA_DDL = {
    "region": """
        CREATE TABLE region (
            r_regionkey UInt32,
            r_name      String,
            r_comment   String
        ) ENGINE = MergeTree ORDER BY r_regionkey
    """,
    "nation": """
        CREATE TABLE nation (
            n_nationkey UInt32,
            n_name      String,
            n_regionkey UInt32,
            n_comment   String
        ) ENGINE = MergeTree ORDER BY n_nationkey
    """,
    "supplier": """
        CREATE TABLE supplier (
            s_suppkey   UInt32,
            s_name      String,
            s_address   String,
            s_nationkey UInt32,
            s_phone     String,
            s_acctbal   Decimal(12,2),
            s_comment   String
        ) ENGINE = MergeTree ORDER BY s_suppkey
    """,
    "part": """
        CREATE TABLE part (
            p_partkey     UInt32,
            p_name        String,
            p_mfgr        String,
            p_brand       String,
            p_type        String,
            p_size        UInt32,
            p_container   String,
            p_retailprice Decimal(12,2),
            p_comment     String
        ) ENGINE = MergeTree ORDER BY p_partkey
    """,
    "partsupp": """
        CREATE TABLE partsupp (
            ps_partkey    UInt32,
            ps_suppkey    UInt32,
            ps_availqty   UInt32,
            ps_supplycost Decimal(12,2),
            ps_comment    String
        ) ENGINE = MergeTree ORDER BY (ps_partkey, ps_suppkey)
    """,
    "customer": """
        CREATE TABLE customer (
            c_custkey    UInt32,
            c_name       String,
            c_address    String,
            c_nationkey  UInt32,
            c_phone      String,
            c_acctbal    Decimal(12,2),
            c_mktsegment String,
            c_comment    String
        ) ENGINE = MergeTree ORDER BY c_custkey
    """,
    "orders": """
        CREATE TABLE orders (
            o_orderkey      UInt32,
            o_custkey       UInt32,
            o_orderstatus   String,
            o_totalprice    Decimal(12,2),
            o_orderdate     Date,
            o_orderpriority String,
            o_clerk         String,
            o_shippriority  UInt32,
            o_comment       String
        ) ENGINE = MergeTree ORDER BY o_orderkey
    """,
    "lineitem": """
        CREATE TABLE lineitem (
            l_orderkey      UInt32,
            l_partkey       UInt32,
            l_suppkey       UInt32,
            l_linenumber    UInt32,
            l_quantity      Decimal(12,2),
            l_extendedprice Decimal(12,2),
            l_discount      Decimal(12,2),
            l_tax           Decimal(12,2),
            l_returnflag    String,
            l_linestatus    String,
            l_shipdate      Date,
            l_commitdate    Date,
            l_receiptdate   Date,
            l_shipinstruct  String,
            l_shipmode      String,
            l_comment       String
        ) ENGINE = MergeTree ORDER BY l_orderkey
    """,
}


def strip_trailing_pipe(tbl_path: Path, csv_path: Path) -> None:
    """
    dbgen writes TPC-H .tbl files with a trailing `|` on every row.
    ClickHouse's CSV reader with `format_csv_delimiter='|'` would
    treat that as a final empty column, breaking the schema match.
    Strip it before loading — same pattern the BigQuery loader uses.
    """
    with tbl_path.open("r", encoding="latin-1") as src, csv_path.open("w", encoding="utf-8") as dst:
        for line in src:
            if line.endswith("|\n"):
                dst.write(line[:-2] + "\n")
            elif line.endswith("|"):
                dst.write(line[:-1])
            else:
                dst.write(line)


def ensure_database(host: str, port: int, user: str, password: str, target_db: str) -> None:
    """CREATE DATABASE IF NOT EXISTS via the default DB. Then connecting
    callers can use the target DB directly."""
    client = clickhouse_connect.get_client(
        host=host, port=port, username=user, password=password, database="default"
    )
    try:
        client.command(f"CREATE DATABASE IF NOT EXISTS `{target_db}`")
        print(f"✅ Database {target_db!r} ready.")
    finally:
        client.close()


def apply_schema(client) -> None:
    """Drop + recreate the 8 tables. Drops first so re-running is
    idempotent."""
    print("Dropping existing TPC-H tables (if any) and recreating from CH-flavoured schema…")
    # Drop the daily-rollup MV first since it depends on `orders`.
    client.command("DROP VIEW IF EXISTS mv_daily_stats")
    client.command("DROP TABLE IF EXISTS daily_order_summary")
    for t in reversed(TPCH_TABLES):
        client.command(f"DROP TABLE IF EXISTS `{t}`")
    for t in TPCH_TABLES:
        client.command(CH_SCHEMA_DDL[t])


def copy_table(client, table: str) -> None:
    tbl_path = DATA_DIR / f"{table}.tbl"
    if not tbl_path.exists():
        print(f"❌ Missing {tbl_path} — run `make tpch-data` first.", file=sys.stderr)
        sys.exit(3)

    csv_path = tbl_path.with_suffix(".ch.csv")
    print(f"  Preparing {table}.tbl → {csv_path.name}…", flush=True)
    strip_trailing_pipe(tbl_path, csv_path)

    # `raw_insert` streams the CSV bytes directly to ClickHouse's HTTP
    # interface — much faster than parsing in Python and pushing rows
    # via the native protocol for million-row tables.
    with csv_path.open("rb") as fh:
        client.raw_insert(
            table,
            insert_block=fh.read(),
            fmt="CSV",
            settings={"format_csv_delimiter": "|"},
        )

    actual = client.query(f"SELECT count() FROM `{table}`").first_row[0]
    expected = SF1_EXPECTED_ROWS[table]
    status = "✓" if actual == expected else "✗"
    print(f"  {status} {table:9s} {actual:>10,d} rows (expected {expected:>10,d})", flush=True)
    csv_path.unlink()

    if actual != expected:
        print(f"❌ Row count mismatch on {table}", file=sys.stderr)
        sys.exit(4)


def apply_augmentations(client) -> None:
    raw = AUGMENT_SQL.read_text(encoding="utf-8")
    # Same comment-strip-before-split pattern as the BigQuery / Postgres
    # loaders — keeps inline `;` in comments from chopping statements.
    no_comments = re.sub(r"--[^\n]*", "", raw)
    statements = [s.strip() for s in no_comments.split(";") if s.strip()]
    print(f"Applying {len(statements)} augmentation statements…", flush=True)
    for i, stmt in enumerate(statements, 1):
        first_line = next((ln.strip() for ln in stmt.splitlines() if ln.strip()), "")
        print(f"  [{i:>2}/{len(statements)}] {first_line[:80]}…", flush=True)
        client.command(stmt)


def main() -> int:
    host = os.environ.get("CH_OSS_HOST", "localhost")
    port = int(os.environ.get("CH_OSS_PORT", "8123"))
    user = os.environ.get("CH_OSS_USER", "default")
    password = os.environ.get("CH_OSS_PASSWORD", "")
    target_db = os.environ.get("CH_OSS_DB", "tpch")

    if not DATA_DIR.exists() or not (DATA_DIR / "lineitem.tbl").exists():
        print(f"❌ TPC-H .tbl files not found in {DATA_DIR}. Run `make tpch-data` first.", file=sys.stderr)
        return 5

    print(f"Loading TPC-H SF1 into clickhouse://{user}@{host}:{port}/{target_db}…")
    ensure_database(host, port, user, password, target_db)

    client = clickhouse_connect.get_client(
        host=host, port=port, username=user, password=password, database=target_db
    )
    try:
        apply_schema(client)
        print("\nLoading 8 TPC-H tables…")
        for table in TPCH_TABLES:
            copy_table(client, table)
        print("\nApplying augmentations…")
        apply_augmentations(client)
    finally:
        client.close()

    print(f"\n✅ Workload setup complete. clickhouse://{user}@{host}:{port}/{target_db} is ready for the ClickHouse OSS migration agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
