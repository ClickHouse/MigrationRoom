"""
MigrationRoom — Databricks workload setup.

Reads DATABRICKS_* credentials from the environment and executes
setup_workload.sql against the partner's SQL warehouse. Copies the built-in
samples.tpch tables into migration_demo.tpch and adds Databricks-specific
decorations (VARIANT, generated column, ARRAY<STRUCT>, MAP,
TIMESTAMP/TIMESTAMP_NTZ, liquid clustering, deletion vectors,
materialized view).

No data download — TPC-H is already inside every Databricks workspace.

Usage:
    pip install -r sources/databricks/scripts/requirements.txt
    set -a; source .env; set +a
    python3 sources/databricks/scripts/setup_workload.py

Environment:
    DATABRICKS_HOST         required — workspace URL or bare hostname
    DATABRICKS_HTTP_PATH    required — e.g. /sql/1.0/warehouses/abc123
    DATABRICKS_TOKEN        required
"""
import os
import re
import sys
from pathlib import Path

SQL_FILE = Path(__file__).parent / "setup_workload.sql"

DIRECTIVE = re.compile(r"^\s*--\s*@(optional|requires)\b[:\s]*(.*)$", re.I)
COMMENT = re.compile(r"^\s*--")


def require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"❌ Missing required env var: {key}", file=sys.stderr)
        sys.exit(2)
    return value


def normalize_host(raw: str) -> str:
    host = raw.strip()
    return host.removeprefix("https://").removeprefix("http://").rstrip("/")


def parse_statements(sql: str) -> list[tuple[str, str, str]]:
    """Split the script into (statement, kind, hint) triples.

    `kind` is 'required' when the statement was preceded by a
    `-- @requires:` directive, 'optional' for `-- @optional:`, else
    'plain'. A directive applies to the next statement only.

    Naive by design: the setup script has no embedded semicolons in
    string literals and no procedural blocks.
    """
    statements: list[tuple[str, str, str]] = []
    buffer: list[str] = []
    kind, hint = "plain", ""

    for line in sql.splitlines():
        directive = DIRECTIVE.match(line)
        if directive:
            kind = "optional" if directive.group(1).lower() == "optional" else "required"
            hint = directive.group(2).strip()
            continue
        if COMMENT.match(line):
            continue
        buffer.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(buffer).strip().rstrip(";").strip()
            if statement:
                statements.append((statement, kind, hint))
            buffer, kind, hint = [], "plain", ""

    tail = "\n".join(buffer).strip().rstrip(";").strip()
    if tail:
        statements.append((tail, kind, hint))
    return statements


def first_line(statement: str, limit: int = 80) -> str:
    for line in statement.splitlines():
        line = line.strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""


def main() -> int:
    from databricks import sql as dbsql

    host = normalize_host(require_env("DATABRICKS_HOST"))
    http_path = require_env("DATABRICKS_HTTP_PATH")
    token = require_env("DATABRICKS_TOKEN")

    statements = parse_statements(SQL_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(statements)} statements from {SQL_FILE.name}.")
    print(f"Connecting to {host} ({http_path})…")

    skipped: list[str] = []
    with dbsql.connect(
        server_hostname=host, http_path=http_path, access_token=token
    ) as conn:
        cur = conn.cursor()
        try:
            for i, (statement, kind, hint) in enumerate(statements, 1):
                print(f"  [{i:>2}/{len(statements)}] {first_line(statement)}", flush=True)
                try:
                    cur.execute(statement)
                except Exception as exc:
                    if kind == "optional":
                        print(f"      ⚠️  skipped: {hint or exc}", flush=True)
                        skipped.append(first_line(statement, 60))
                        continue
                    if kind == "required":
                        print(f"\n❌ {hint}", file=sys.stderr)
                        print(f"   Statement: {first_line(statement)}", file=sys.stderr)
                        print(f"   Error: {exc}", file=sys.stderr)
                        return 3
                    print(f"\n❌ Statement failed: {first_line(statement)}", file=sys.stderr)
                    print(f"   Error: {exc}", file=sys.stderr)
                    return 1
        finally:
            cur.close()

    print("\n✅ Workload setup complete. migration_demo.tpch is ready.")
    if skipped:
        print(f"⚠️  {len(skipped)} optional statement(s) skipped:")
        for label in skipped:
            print(f"     - {label}")
    print("   Set DATABRICKS_NAMESPACE=migration_demo.tpch in .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
