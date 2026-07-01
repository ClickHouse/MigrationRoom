"""
MigrationRoom — TPC-H workload loader for BigQuery.

Loads the TPC-H SF1 .tbl files produced by `make tpch-data` into
${BIGQUERY_PROJECT}.${BIGQUERY_DATASET} and applies the cross-source
augmentation contract (see ../augmentations.md) in BigQuery-native form:

    1. orders.order_metadata STRUCT<...>     (semi-structured)
    2. lineitem.delivery_at TIMESTAMP        + PARTITION BY + CLUSTER BY
    3. daily_order_summary MATERIALIZED VIEW (pre-aggregated revenue)
    4. customer.contact_addresses ARRAY<STRUCT<...>>

Usage:
    pip install -r workloads/tpch/bigquery/requirements.txt
    set -a; source .env; set +a
    python3 workloads/tpch/bigquery/load.py

Environment:
    BIGQUERY_PROJECT   required — GCP project ID
    BIGQUERY_DATASET   default: migration_demo
    BIGQUERY_LOCATION  default: US
    GOOGLE_APPLICATION_CREDENTIALS  required — path to service-account JSON
                                    with BigQuery Data Editor + Job User
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from google.cloud import bigquery

REPO_ROOT = Path(__file__).resolve().parents[2].parent  # workloads/tpch/bigquery → repo root
WORKLOAD_DIR = Path(__file__).resolve().parents[1]      # workloads/tpch/
DATA_DIR = WORKLOAD_DIR / "data" / "sf1"
SCHEMA_SQL = WORKLOAD_DIR / "schema.sql"
AUGMENT_SQL = Path(__file__).resolve().parent / "augmentations.sql"

# TPC-H .tbl files are pipe-delimited, with a trailing pipe on every row
# (an oddity of the official dbgen format). Each loader strips that
# trailing pipe before treating the row as CSV.
TPCH_TABLES = [
    "region", "nation", "supplier", "part",
    "partsupp", "customer", "orders", "lineitem",
]

# BigQuery-flavoured types per TPC-H column. Keys match the column names in
# workloads/tpch/schema.sql.
BQ_SCHEMA: dict[str, list[bigquery.SchemaField]] = {
    "region": [
        bigquery.SchemaField("r_regionkey", "INT64"),
        bigquery.SchemaField("r_name", "STRING"),
        bigquery.SchemaField("r_comment", "STRING"),
    ],
    "nation": [
        bigquery.SchemaField("n_nationkey", "INT64"),
        bigquery.SchemaField("n_name", "STRING"),
        bigquery.SchemaField("n_regionkey", "INT64"),
        bigquery.SchemaField("n_comment", "STRING"),
    ],
    "supplier": [
        bigquery.SchemaField("s_suppkey", "INT64"),
        bigquery.SchemaField("s_name", "STRING"),
        bigquery.SchemaField("s_address", "STRING"),
        bigquery.SchemaField("s_nationkey", "INT64"),
        bigquery.SchemaField("s_phone", "STRING"),
        bigquery.SchemaField("s_acctbal", "NUMERIC"),
        bigquery.SchemaField("s_comment", "STRING"),
    ],
    "part": [
        bigquery.SchemaField("p_partkey", "INT64"),
        bigquery.SchemaField("p_name", "STRING"),
        bigquery.SchemaField("p_mfgr", "STRING"),
        bigquery.SchemaField("p_brand", "STRING"),
        bigquery.SchemaField("p_type", "STRING"),
        bigquery.SchemaField("p_size", "INT64"),
        bigquery.SchemaField("p_container", "STRING"),
        bigquery.SchemaField("p_retailprice", "NUMERIC"),
        bigquery.SchemaField("p_comment", "STRING"),
    ],
    "partsupp": [
        bigquery.SchemaField("ps_partkey", "INT64"),
        bigquery.SchemaField("ps_suppkey", "INT64"),
        bigquery.SchemaField("ps_availqty", "INT64"),
        bigquery.SchemaField("ps_supplycost", "NUMERIC"),
        bigquery.SchemaField("ps_comment", "STRING"),
    ],
    "customer": [
        bigquery.SchemaField("c_custkey", "INT64"),
        bigquery.SchemaField("c_name", "STRING"),
        bigquery.SchemaField("c_address", "STRING"),
        bigquery.SchemaField("c_nationkey", "INT64"),
        bigquery.SchemaField("c_phone", "STRING"),
        bigquery.SchemaField("c_acctbal", "NUMERIC"),
        bigquery.SchemaField("c_mktsegment", "STRING"),
        bigquery.SchemaField("c_comment", "STRING"),
    ],
    "orders": [
        bigquery.SchemaField("o_orderkey", "INT64"),
        bigquery.SchemaField("o_custkey", "INT64"),
        bigquery.SchemaField("o_orderstatus", "STRING"),
        bigquery.SchemaField("o_totalprice", "NUMERIC"),
        bigquery.SchemaField("o_orderdate", "DATE"),
        bigquery.SchemaField("o_orderpriority", "STRING"),
        bigquery.SchemaField("o_clerk", "STRING"),
        bigquery.SchemaField("o_shippriority", "INT64"),
        bigquery.SchemaField("o_comment", "STRING"),
    ],
    "lineitem": [
        bigquery.SchemaField("l_orderkey", "INT64"),
        bigquery.SchemaField("l_partkey", "INT64"),
        bigquery.SchemaField("l_suppkey", "INT64"),
        bigquery.SchemaField("l_linenumber", "INT64"),
        bigquery.SchemaField("l_quantity", "NUMERIC"),
        bigquery.SchemaField("l_extendedprice", "NUMERIC"),
        bigquery.SchemaField("l_discount", "NUMERIC"),
        bigquery.SchemaField("l_tax", "NUMERIC"),
        bigquery.SchemaField("l_returnflag", "STRING"),
        bigquery.SchemaField("l_linestatus", "STRING"),
        bigquery.SchemaField("l_shipdate", "DATE"),
        bigquery.SchemaField("l_commitdate", "DATE"),
        bigquery.SchemaField("l_receiptdate", "DATE"),
        bigquery.SchemaField("l_shipinstruct", "STRING"),
        bigquery.SchemaField("l_shipmode", "STRING"),
        bigquery.SchemaField("l_comment", "STRING"),
    ],
}

# Expected SF1 row counts — used for the post-load sanity check.
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


def require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        print(f"❌ Missing required env var: {key}", file=sys.stderr)
        sys.exit(2)
    return val


def strip_trailing_pipe(tbl_path: Path, csv_path: Path) -> None:
    """
    dbgen writes TPC-H .tbl files with a trailing `|` on every row.
    BigQuery's CSV loader interprets that as an extra empty column. Strip
    it before loading.
    """
    with tbl_path.open("r", encoding="latin-1") as src, csv_path.open("w", encoding="utf-8") as dst:
        for line in src:
            if line.endswith("|\n"):
                dst.write(line[:-2] + "\n")
            elif line.endswith("|"):
                dst.write(line[:-1])
            else:
                dst.write(line)


def load_table(client: bigquery.Client, dataset: str, table: str) -> None:
    tbl_path = DATA_DIR / f"{table}.tbl"
    if not tbl_path.exists():
        print(f"❌ Missing {tbl_path} — run `make tpch-data` first.", file=sys.stderr)
        sys.exit(3)

    table_id = f"{dataset}.{table}"
    csv_path = tbl_path.with_suffix(".csv")
    print(f"  Preparing {table}.tbl → {csv_path.name}…", flush=True)
    strip_trailing_pipe(tbl_path, csv_path)

    job_config = bigquery.LoadJobConfig(
        schema=BQ_SCHEMA[table],
        source_format=bigquery.SourceFormat.CSV,
        field_delimiter="|",
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        # dbgen output is already clean — no header row, no quoting.
        skip_leading_rows=0,
    )

    with csv_path.open("rb") as fh:
        job = client.load_table_from_file(fh, table_id, job_config=job_config)
    job.result()  # block until done

    actual = client.get_table(table_id).num_rows
    expected = SF1_EXPECTED_ROWS[table]
    status = "✓" if actual == expected else "✗"
    print(f"  {status} {table:9s} {actual:>10,d} rows (expected {expected:>10,d})", flush=True)
    csv_path.unlink()  # cleanup the temporary stripped CSV

    if actual != expected:
        print(f"❌ Row count mismatch on {table}", file=sys.stderr)
        sys.exit(4)


def apply_augmentations(client: bigquery.Client, project: str, dataset: str) -> None:
    sql = AUGMENT_SQL.read_text(encoding="utf-8")
    # The augmentations.sql file references `${BIGQUERY_PROJECT}` and
    # `${BIGQUERY_DATASET}` placeholders — interpolate them here so the
    # SQL itself can stay engine-readable.
    sql = sql.replace("${BIGQUERY_PROJECT}", project).replace("${BIGQUERY_DATASET}", dataset)

    # Strip `--` line comments BEFORE splitting on `;`. The naive
    # split-on-semicolon approach breaks when a comment contains an
    # inline `;` (e.g. "-- PARTITION BY ...; CLUSTER BY is BigQuery's"
    # would chop the second half of the comment into a fresh "stmt"
    # starting with `CLUSTER BY`). Removing comments first avoids the
    # quoting / commented-block edge cases entirely.
    sql_no_comments = re.sub(r"--[^\n]*", "", sql)

    # Split on `;` at end-of-statement (BigQuery scripts support multi-
    # statement bodies, but each step here is independent and benefits
    # from clearer per-statement error reporting).
    statements = [s.strip() for s in sql_no_comments.split(";") if s.strip()]
    print(f"Applying {len(statements)} augmentation statements…", flush=True)
    for i, stmt in enumerate(statements, 1):
        first_line = next((ln.strip() for ln in stmt.splitlines() if ln.strip() and not ln.strip().startswith("--")), "")
        print(f"  [{i:>2}/{len(statements)}] {first_line[:80]}…", flush=True)
        client.query(stmt).result()


def main() -> int:
    project = require_env("BIGQUERY_PROJECT")
    dataset_id = os.environ.get("BIGQUERY_DATASET", "migration_demo")
    location = os.environ.get("BIGQUERY_LOCATION", "US")

    if not DATA_DIR.exists() or not (DATA_DIR / "lineitem.tbl").exists():
        print(f"❌ TPC-H .tbl files not found in {DATA_DIR}. Run `make tpch-data` first.", file=sys.stderr)
        return 5

    print(f"Loading TPC-H SF1 into {project}.{dataset_id} (location={location})…")
    client = bigquery.Client(project=project, location=location)

    # Create dataset if missing.
    dataset_ref = bigquery.Dataset(f"{project}.{dataset_id}")
    dataset_ref.location = location
    client.create_dataset(dataset_ref, exists_ok=True)
    fq_dataset = f"{project}.{dataset_id}"
    print(f"✅ Dataset ready: {fq_dataset}")

    print("\nLoading 8 TPC-H tables…")
    for table in TPCH_TABLES:
        load_table(client, fq_dataset, table)

    print("\nApplying augmentations…")
    apply_augmentations(client, project, dataset_id)

    print(f"\n✅ Workload setup complete. {fq_dataset} is ready for the BigQuery migration agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
