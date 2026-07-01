"""
MigrationRoom — TPC-H workload loader for PostgreSQL.

Loads the TPC-H SF1 .tbl files produced by `make tpch-data` into a
dedicated `tpch` database on the target Postgres instance and applies
the cross-source augmentation contract (see ../augmentations.md) in
Postgres-native form:

    1. orders.order_metadata JSONB                      (semi-structured)
    2. lineitem.delivery_at TIMESTAMPTZ + BRIN index    (TZ + index)
    3. daily_order_summary MATERIALIZED VIEW            (pre-aggregated)
    4. customer.contact_addresses JSONB                 (nested array)

Coexistence strategy: writes to a NEW `tpch` database alongside the
bundled `ecommerce` workload. The existing partner-facing demo
(sources/postgres/scripts/migrate.py + the e-commerce schema) is
untouched. Partners point the migration agent at TPC-H by setting
`POSTGRES_DB=tpch` in their `.env`.

Usage:
    pip install -r workloads/tpch/postgres/requirements.txt
    set -a; source .env; set +a
    python3 workloads/tpch/postgres/load.py

Environment:
    POSTGRES_HOST       default: localhost  (use 'postgres' from inside docker)
    POSTGRES_PORT       default: 5432
    POSTGRES_USER       default: playground
    POSTGRES_PASSWORD   default: playground
    POSTGRES_DB         default: tpch       (the DB this script creates)
    POSTGRES_ADMIN_DB   default: postgres   (used to bootstrap CREATE DATABASE)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql

WORKLOAD_DIR = Path(__file__).resolve().parents[1]   # workloads/tpch/
DATA_DIR = WORKLOAD_DIR / "data" / "sf1"
SCHEMA_SQL = WORKLOAD_DIR / "schema.sql"
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


def strip_trailing_pipe(tbl_path: Path, csv_path: Path) -> None:
    """
    dbgen writes TPC-H .tbl files with a trailing `|` on every row.
    Postgres COPY treats the trailing pipe as a final empty column,
    which doesn't match our schema. Strip it before loading — same
    pattern the BigQuery loader uses.
    """
    with tbl_path.open("r", encoding="latin-1") as src, csv_path.open("w", encoding="utf-8") as dst:
        for line in src:
            if line.endswith("|\n"):
                dst.write(line[:-2] + "\n")
            elif line.endswith("|"):
                dst.write(line[:-1])
            else:
                dst.write(line)


def ensure_database(admin_dsn: dict, target_db: str) -> None:
    """Create the target database if it doesn't exist. Connect to the
    admin DB (`postgres` by default) since CREATE DATABASE cannot run
    inside a transaction tied to the target."""
    conn = psycopg2.connect(**admin_dsn)
    conn.autocommit = True  # CREATE DATABASE forbids transactions
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
            print(f"✅ Created database {target_db!r}.")
        else:
            print(f"✅ Database {target_db!r} already exists.")
    finally:
        conn.close()


def apply_schema(conn: "psycopg2.extensions.connection") -> None:
    """Drop + recreate the 8 tables. `schema.sql` is engine-neutral with
    Postgres-flavoured types — runs as-is, no dialect translation.
    Drops first so re-running is idempotent."""
    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    cur = conn.cursor()
    print("Dropping existing TPC-H tables (if any) and recreating from schema.sql…")
    # Drop in reverse dep order to be safe (we have no FKs, but be tidy).
    for t in reversed(TPCH_TABLES):
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(t)))
    # `daily_order_summary` is a materialized view — augmentation step
    # creates it but it may persist from a previous run; drop just in
    # case (CASCADE on orders above won't catch the MV since the MV is
    # rebuilt in apply_augmentations).
    cur.execute("DROP MATERIALIZED VIEW IF EXISTS daily_order_summary CASCADE")
    cur.execute(raw)
    conn.commit()


def copy_table(conn: "psycopg2.extensions.connection", table: str) -> None:
    tbl_path = DATA_DIR / f"{table}.tbl"
    if not tbl_path.exists():
        print(f"❌ Missing {tbl_path} — run `make tpch-data` first.", file=sys.stderr)
        sys.exit(3)

    csv_path = tbl_path.with_suffix(".pg.csv")
    print(f"  Preparing {table}.tbl → {csv_path.name}…", flush=True)
    strip_trailing_pipe(tbl_path, csv_path)

    cur = conn.cursor()
    with csv_path.open("r", encoding="utf-8") as fh:
        cur.copy_expert(
            sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, DELIMITER '|', HEADER false)")
                .format(sql.Identifier(table)),
            fh,
        )
    conn.commit()

    cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
    actual = cur.fetchone()[0]
    expected = SF1_EXPECTED_ROWS[table]
    status = "✓" if actual == expected else "✗"
    print(f"  {status} {table:9s} {actual:>10,d} rows (expected {expected:>10,d})", flush=True)
    csv_path.unlink()

    if actual != expected:
        print(f"❌ Row count mismatch on {table}", file=sys.stderr)
        sys.exit(4)


def apply_augmentations(conn: "psycopg2.extensions.connection") -> None:
    raw = AUGMENT_SQL.read_text(encoding="utf-8")
    # Strip `--` line comments BEFORE splitting on `;` — same caveat as
    # the BigQuery loader (inline `;` in comments would chop statements
    # otherwise).
    no_comments = re.sub(r"--[^\n]*", "", raw)
    statements = [s.strip() for s in no_comments.split(";") if s.strip()]
    print(f"Applying {len(statements)} augmentation statements…", flush=True)
    cur = conn.cursor()
    for i, stmt in enumerate(statements, 1):
        first_line = next((ln.strip() for ln in stmt.splitlines() if ln.strip()), "")
        print(f"  [{i:>2}/{len(statements)}] {first_line[:80]}…", flush=True)
        cur.execute(stmt)
    conn.commit()


def main() -> int:
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    user = os.environ.get("POSTGRES_USER", "playground")
    password = os.environ.get("POSTGRES_PASSWORD", "playground")
    target_db = os.environ.get("POSTGRES_DB", "tpch")
    admin_db = os.environ.get("POSTGRES_ADMIN_DB", "postgres")

    if not DATA_DIR.exists() or not (DATA_DIR / "lineitem.tbl").exists():
        print(f"❌ TPC-H .tbl files not found in {DATA_DIR}. Run `make tpch-data` first.", file=sys.stderr)
        return 5

    admin_dsn = dict(host=host, port=port, user=user, password=password, dbname=admin_db)
    print(f"Loading TPC-H SF1 into postgres://{user}@{host}:{port}/{target_db}…")
    ensure_database(admin_dsn, target_db)

    target_dsn = dict(host=host, port=port, user=user, password=password, dbname=target_db)
    conn = psycopg2.connect(**target_dsn)
    try:
        apply_schema(conn)
        print("\nLoading 8 TPC-H tables…")
        for table in TPCH_TABLES:
            copy_table(conn, table)
        print("\nApplying augmentations…")
        apply_augmentations(conn)
    finally:
        conn.close()

    print(f"\n✅ Workload setup complete. postgres://{user}@{host}:{port}/{target_db} is ready for the Postgres migration agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
