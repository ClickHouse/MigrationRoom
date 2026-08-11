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
    MCP_ALLOWED_HOSTS       optional — comma-separated Host allow-list,
                            default "databricks-mcp:*,localhost:*,127.0.0.1:*"
"""
from __future__ import annotations

import functools
import os
import re
from contextlib import contextmanager
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from sql_guard import SqlNotAllowed, guard

# The SDK's SSE transport enforces DNS-rebinding protection, and its default
# allow-list is EMPTY — every Host header is rejected with 421 except none.
# That default is wrong for us in a way that hides itself: the healthcheck
# probes `localhost:8000`, so an unconfigured server reports *healthy* while
# LibreChat's `http://databricks-mcp:8000/sse` gets a 421 and the agent ends
# up with no Databricks tools.
#
# Protection stays ON rather than being switched off: compose publishes this
# port to the host (8008:8000), so a page in the user's browser can reach it,
# which is precisely the attack DNS-rebinding protection exists to stop. An
# allow-list still blocks it — an attacker-controlled name resolving to
# 127.0.0.1 arrives as `evil.example:8008` and fails — while admitting the
# three names that legitimately address this server. `host:*` accepts any
# port on that host; it does not widen the set of hosts.
#
# Note this differs from clickhousectl-mcp, which runs the standalone
# `fastmcp` package (no such middleware) rather than the SDK's bundled one.
_DEFAULT_ALLOWED_HOSTS = "databricks-mcp:*,localhost:*,127.0.0.1:*"
_ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("MCP_ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOSTS).split(",")
    if h.strip()
]

mcp = FastMCP(
    "databricks-source",
    transport_security=TransportSecuritySettings(
        allowed_hosts=_ALLOWED_HOSTS,
        # Origin is absent on LibreChat's server-side requests (absent passes).
        # Mirroring the host list keeps a browser-based MCP inspector working
        # without admitting a cross-origin caller.
        allowed_origins=[f"http://{h}" for h in _ALLOWED_HOSTS]
        + [f"https://{h}" for h in _ALLOWED_HOSTS],
    ),
)

# Materialized views (and streaming tables) in Unity Catalog are backed by a
# pipeline that creates its own internal tables *in the same schema as the
# view* — Databricks' docs confirm these "appear in
# system.information_schema.tables but are not visible in Catalog Explorer
# or other workspace UI surfaces." There is no documented metadata column
# (no is_internal flag, and table_type is plain "MANAGED" like any other
# managed table) that distinguishes them from real user tables, so a
# name-based filter is the only signal information_schema.tables offers.
#
# Observed live against `migration_demo.tpch` (2026-08-06), backing
# `daily_order_summary`, a serverless materialized view:
#   __materialization_mat_722efdfe_b99a_4f8b_b06e_9cc1f8de6d68_daily_order_summary_1
#   event_log_722efdfe_b99a_4f8b_b06e_9cc1f8de6d68
# Both names are suffixed with the pipeline's UUID (hyphens become
# underscores, since table names can't contain hyphens). The pattern below
# requires that UUID shape rather than matching any `event_log*` or
# `__materialization*` prefix, so a legitimate user table such as
# `event_log_orders` or `__materialization_notes` is never hidden — only
# names carrying an actual 8-4-4-4-12 hex UUID segment match.
_UUID = r"[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{12}"
_MV_PIPELINE_INTERNAL_RE = re.compile(
    rf"^(?:__materialization_mat_{_UUID}_.+_\d+|event_log_{_UUID})$"
)


def _is_mv_pipeline_internal(table_name: str) -> bool:
    """True for a materialized-view/streaming-table pipeline's own backing
    materialization table or event log — Databricks implementation detail,
    not user data. See the comment on `_MV_PIPELINE_INTERNAL_RE` above.
    """
    return bool(_MV_PIPELINE_INTERNAL_RE.match(table_name or ""))


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


def _threaded(fn):
    """Run a blocking tool body in a worker thread.

    FastMCP 1.x dispatches tools with `await fn(...)` if the function is a
    coroutine and a bare `fn(...)` otherwise — there is no thread offload on
    the tool path. Every tool here talks Thrift/HTTPS to the warehouse, so as
    a plain `def` each one froze the entire event loop for its duration: tool
    calls could not overlap, pings went unanswered, and already-computed
    responses sat unflushed. Because LibreChat measures its 60 s tool timeout
    from the moment it *sends* a call, calls queued behind others spent their
    whole budget waiting and failed with MCP error -32001 — while the server
    logged nothing wrong, having answered each one in a few seconds.

    Measured on a warm 2X-Small warehouse: four concurrent `describe_table`
    calls returned at 4.4 s, 9.1 s, 13.4 s and 17.5 s (a perfect staircase),
    and a ping issued during a call took 4.03 s against 0.00 s when idle.

    `functools.wraps` matters beyond cosmetics: FastMCP builds the tool's
    JSON schema from `inspect.signature`, which follows `__wrapped__`, and
    takes the model-facing description from `__doc__`. Both therefore come
    from the wrapped function, while `iscoroutinefunction` sees the async
    wrapper and takes the awaiting branch.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper


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
@_threaded
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
@_threaded
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
@_threaded
def list_tables(catalog: str, schema: str) -> list[dict[str, Any]]:
    """List tables in `catalog`.`schema` with Delta size metadata.

    Excludes a materialized view/streaming table's own backing
    materialization table and event log (see `_is_mv_pipeline_internal`) —
    Databricks implementation detail an agent should not inventory or try to
    migrate. The `MATERIALIZED_VIEW` row itself is kept.

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
        tables = [
            row for row in tables
            if not _is_mv_pipeline_internal(row.get("table_name", ""))
        ]
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
@_threaded
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
@_threaded
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
