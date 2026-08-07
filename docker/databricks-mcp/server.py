"""MigrationRoom — Databricks source MCP server.

Read-only introspection and SELECT access to ONE Databricks SQL warehouse,
exposed over MCP/SSE so the migration agent can discover the source schema
without writing Python.

Why this exists rather than an off-the-shelf package: Databricks' own
`databricks-mcp` PyPI package is an OAuth helper for their *hosted* MCP
servers (Unity Catalog functions, vector search, Genie) — there is no
official introspect-and-SELECT MCP for a SQL warehouse. Authoring the tool
schemas ourselves also keeps them free of the JSON-Schema keywords that
Gemini's function-calling API rejects, so this server needs no shim (unlike
snowflake-source).

Environment:
    DATABRICKS_HOST         required — workspace URL or bare hostname
    DATABRICKS_HTTP_PATH    required — e.g. /sql/1.0/warehouses/abc123
    DATABRICKS_TOKEN        required — PAT for a read-only principal
    DATABRICKS_NAMESPACE    optional — "<catalog>.<schema>" default scope
    MCP_PORT                optional — default 8000
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from sql_guard import SqlNotAllowed, guard

mcp = FastMCP("databricks-source")


def _host() -> str:
    """Bare hostname — the connector rejects a scheme or trailing slash."""
    raw = os.environ["DATABRICKS_HOST"].strip()
    return raw.removeprefix("https://").removeprefix("http://").rstrip("/")


@contextmanager
def _cursor():
    """One short-lived connection + cursor per tool call.

    Deliberately not pooled: LibreChat holds the SSE session open for the
    whole conversation, and a warehouse that auto-stops would leave a stale
    connection behind. Reconnecting costs ~1 s and is far less confusing
    than a silently dead handle.
    """
    from databricks import sql as dbsql

    conn = dbsql.connect(
        server_hostname=_host(),
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )
    try:
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()
    finally:
        conn.close()


def _rows(cur) -> list[dict[str, Any]]:
    columns = [c[0] for c in cur.description or []]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _ident(name: str) -> str:
    """Backtick-quote one identifier part, rejecting embedded backticks.

    Identifiers arrive as tool arguments from the model, so they are
    untrusted input even though they are not user-facing.
    """
    cleaned = (name or "").strip().strip("`")
    if not cleaned or "`" in cleaned:
        raise ValueError(f"invalid identifier: {name!r}")
    return f"`{cleaned}`"


@mcp.tool()
def list_catalogs() -> list[dict[str, Any]]:
    """List Unity Catalog catalogs visible to this principal."""
    with _cursor() as cur:
        cur.execute(
            "SELECT catalog_name, comment "
            "FROM system.information_schema.catalogs "
            "ORDER BY catalog_name"
        )
        return _rows(cur)


@mcp.tool()
def list_schemas(catalog: str) -> list[dict[str, Any]]:
    """List schemas in `catalog`."""
    with _cursor() as cur:
        cur.execute(
            "SELECT schema_name, comment "
            "FROM system.information_schema.schemata "
            "WHERE catalog_name = ? "
            "ORDER BY schema_name",
            [catalog],
        )
        return _rows(cur)


@mcp.tool()
def list_tables(catalog: str, schema: str) -> list[dict[str, Any]]:
    """List tables in `catalog`.`schema` with Delta size metadata.

    Row counts are NOT included: Delta metadata doesn't carry them and a
    per-table COUNT(*) would make this call slow. Get them with one
    UNION ALL count query via run_select_query instead.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT table_name, table_type, comment "
            "FROM system.information_schema.tables "
            "WHERE table_catalog = ? "
            "  AND table_schema = ? "
            "ORDER BY table_name",
            [catalog, schema],
        )
        tables = _rows(cur)
        for row in tables:
            row["sizeInBytes"] = None
            row["numFiles"] = None
            if row.get("table_type") not in (None, "MANAGED", "EXTERNAL"):
                continue
            fq = f"{_ident(catalog)}.{_ident(schema)}.{_ident(row['table_name'])}"
            try:
                cur.execute(f"DESCRIBE DETAIL {fq}")
                detail = _rows(cur)
            except Exception:
                # Views and non-Delta tables have no DESCRIBE DETAIL.
                continue
            if detail:
                row["sizeInBytes"] = detail[0].get("sizeInBytes")
                row["numFiles"] = detail[0].get("numFiles")
        return tables


@mcp.tool()
def describe_table(catalog: str, schema: str, table: str) -> dict[str, Any]:
    """Full schema plus Delta detail for one table.

    Returns columns, plus clustering/partition columns, table features,
    deletion-vector state, and recent history — the source-specific
    features the migration has to make decisions about.
    """
    fq = f"{_ident(catalog)}.{_ident(schema)}.{_ident(table)}"
    out: dict[str, Any] = {"table": f"{catalog}.{schema}.{table}"}
    with _cursor() as cur:
        cur.execute(f"DESCRIBE TABLE EXTENDED {fq}")
        out["describe_extended"] = _rows(cur)
        try:
            cur.execute(f"DESCRIBE DETAIL {fq}")
            out["detail"] = _rows(cur)
        except Exception as exc:
            out["detail"] = {"unavailable": str(exc)}
        try:
            cur.execute(f"DESCRIBE HISTORY {fq} LIMIT 5")
            out["history"] = _rows(cur)
        except Exception as exc:
            out["history"] = {"unavailable": str(exc)}
    return out


@mcp.tool()
def run_select_query(sql: str, max_rows: int = 200) -> list[dict[str, Any]]:
    """Run ONE read-only statement (SELECT / WITH / SHOW / DESCRIBE /
    EXPLAIN) and return its rows.

    A LIMIT is applied when the statement has none. Mutations and
    multi-statement input are refused — run DDL against the target with
    the clickhousectl MCP.
    """
    try:
        statement = guard(sql, max_rows=max_rows)
    except SqlNotAllowed as exc:
        raise ValueError(str(exc)) from exc
    with _cursor() as cur:
        cur.execute(statement)
        return _rows(cur)


if __name__ == "__main__":
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("MCP_PORT", "8000"))
    mcp.run(transport="sse")
