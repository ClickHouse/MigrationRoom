"""
MigrationRoom — Snowflake workload setup.

Reads SNOWFLAKE_* credentials from the environment and executes
setup_workload.sql against the partner's Snowflake account. Copies the
TPC-H sample tables into MIGRATION_DEMO.RETAIL and adds Snowflake-specific
decorations (VARIANT, TIMESTAMP_TZ, Clustering Key, Stream, Dynamic Table).

No data download / SSL handling — TPC-H is already inside Snowflake.

Usage:
    pip install -r sources/snowflake/scripts/requirements.txt
    set -a; source .env; set +a
    python3 sources/snowflake/scripts/setup_workload.py

Environment:
    SNOWFLAKE_ACCOUNT       required
    SNOWFLAKE_USER          required
    SNOWFLAKE_PASSWORD      required
    SNOWFLAKE_WAREHOUSE     default: COMPUTE_WH
    SNOWFLAKE_ROLE          optional
"""
import os
import re
import sys
from pathlib import Path

import snowflake.connector

SQL_FILE = Path(__file__).parent / "setup_workload.sql"


def require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        print(f"❌ Missing required env var: {key}", file=sys.stderr)
        sys.exit(2)
    return val


def split_statements(sql: str) -> list[str]:
    """
    Split a SQL script into individual statements by ';' at end-of-line,
    ignoring lines that start with '--' and skipping empty results.
    Naive but sufficient for the setup script (no embedded semicolons in
    quoted strings, no procedural code).
    """
    # Strip line comments
    cleaned = "\n".join(
        line for line in sql.splitlines()
        if not re.match(r"^\s*--", line)
    )
    parts = [s.strip() for s in cleaned.split(";")]
    return [s for s in parts if s]


def first_line(stmt: str, limit: int = 80) -> str:
    """Return a short label for a statement (first non-blank line)."""
    for line in stmt.splitlines():
        line = line.strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""


def main() -> int:
    account = require_env("SNOWFLAKE_ACCOUNT")
    user = require_env("SNOWFLAKE_USER")
    password = require_env("SNOWFLAKE_PASSWORD")
    warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
    role = os.environ.get("SNOWFLAKE_ROLE") or None

    print(f"Connecting to {account} as {user} (warehouse={warehouse})…")
    conn_kwargs = dict(
        account=account,
        user=user,
        password=password,
        warehouse=warehouse,
        paramstyle="qmark",
    )
    if role:
        conn_kwargs["role"] = role

    sql = SQL_FILE.read_text(encoding="utf-8")
    statements = split_statements(sql)
    print(f"Loaded {len(statements)} statements from {SQL_FILE.name}.")

    with snowflake.connector.connect(**conn_kwargs) as conn:
        cur = conn.cursor()
        try:
            for i, stmt in enumerate(statements, 1):
                print(f"  [{i:>2}/{len(statements)}] {first_line(stmt)}", flush=True)
                cur.execute(stmt)
        finally:
            cur.close()

    print("✅ Workload setup complete. MIGRATION_DEMO.RETAIL is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
