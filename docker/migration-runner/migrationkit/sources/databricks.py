"""Databricks SQL warehouse as a migration source.

Mirrors SnowflakeSource: direct batch reads for small tables, plus an
S3 staging path for large facts. Unity Catalog is three-level
(catalog.schema.table) but the playground models one "source database"
per run, so DATABRICKS_NAMESPACE carries "<catalog>.<schema>" and
`self.database` is that dotted string.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, TYPE_CHECKING

from .base import Source, UnloadResult

if TYPE_CHECKING:
    from ..staging.s3 import S3Stage


def split_namespace(namespace: str) -> tuple[str, str]:
    """Split `"<catalog>.<schema>"` into its two parts."""
    parts = [p.strip() for p in (namespace or "").split(".") if p.strip()]
    if len(parts) != 2:
        raise ValueError(
            "DATABRICKS_NAMESPACE must be '<catalog>.<schema>' "
            f"(e.g. migration_demo.tpch), got {namespace!r}"
        )
    return parts[0], parts[1]


def normalize_host(raw: str) -> str:
    """Bare hostname. The connector's `server_hostname` rejects a scheme."""
    host = (raw or "").strip()
    return host.removeprefix("https://").removeprefix("http://").rstrip("/")


def parquet_only(objects: list) -> list:
    """Keep only Parquet part-files.

    Databricks' commit protocol writes `_committed_*`, `_started_*` and
    `_SUCCESS` markers alongside the data. Counting them would inflate the
    file count and byte total the dashboard shows for the unload phase.
    """
    return [o for o in objects if o.key.lower().endswith(".parquet")]


class DatabricksSource(Source):
    source_type = "databricks"

    def __init__(
        self,
        server_hostname: str,
        http_path: str,
        access_token: str,
        catalog: str | None = None,
        schema: str | None = None,
    ) -> None:
        from databricks import sql as dbsql

        self.catalog = catalog
        self.schema = schema
        self.database = f"{catalog}.{schema}" if catalog and schema else None
        self._host = normalize_host(server_hostname)
        self._token = access_token
        self._conn = dbsql.connect(
            server_hostname=self._host,
            http_path=http_path,
            access_token=access_token,
            catalog=catalog,
            schema=schema,
        )

    @classmethod
    def from_env(cls) -> "DatabricksSource":
        catalog = schema = None
        namespace = os.environ.get("DATABRICKS_NAMESPACE")
        if namespace:
            catalog, schema = split_namespace(namespace)
        return cls(
            server_hostname=os.environ["DATABRICKS_HOST"],
            http_path=os.environ["DATABRICKS_HTTP_PATH"],
            access_token=os.environ["DATABRICKS_TOKEN"],
            catalog=catalog,
            schema=schema,
        )

    @classmethod
    def list_databases_from_env(cls) -> list[str]:
        """Return `catalog.schema` pairs visible to the env credentials.

        Backs the dashboard's source-database dropdown. Every value is
        also a usable SQL prefix, which is why the pair is returned as one
        dotted string rather than a nested structure.
        """
        from databricks import sql as dbsql

        conn = dbsql.connect(
            server_hostname=normalize_host(os.environ["DATABRICKS_HOST"]),
            http_path=os.environ["DATABRICKS_HTTP_PATH"],
            access_token=os.environ["DATABRICKS_TOKEN"],
        )
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT catalog_name, schema_name "
                    "FROM system.information_schema.schemata "
                    "WHERE schema_name <> 'information_schema' "
                    "ORDER BY catalog_name, schema_name"
                )
                return [f"{row[0]}.{row[1]}" for row in cur.fetchall()]
            finally:
                cur.close()
        finally:
            conn.close()

    def _fq(self, table: str) -> str:
        """Fully-qualify a bare table name against the run's namespace."""
        if "." in table:
            return table
        if not (self.catalog and self.schema):
            raise ValueError(
                f"table {table!r} is unqualified and no DATABRICKS_NAMESPACE "
                f"is set — pass '<catalog>.<schema>.{table}' instead"
            )
        return f"{self.catalog}.{self.schema}.{table}"

    def count_rows(self, query: str) -> int:
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT count(*) FROM ({query})")
            (n,) = cur.fetchone()
            return int(n)
        finally:
            cur.close()

    def iter_batches(
        self, query: str, batch_size: int
    ) -> Iterator[list[dict[str, Any]]]:
        cur = self._conn.cursor()
        try:
            cur.execute(query)
            columns = [c[0].lower() for c in cur.description]
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    return
                yield [dict(zip(columns, row)) for row in rows]
        finally:
            cur.close()

    def execute_and_count(self, sql: str) -> tuple[int, float | None, float]:
        cur = self._conn.cursor()
        try:
            t0 = time.monotonic()
            cur.execute(sql)
            rows = cur.fetchall()
            wall_ms = (time.monotonic() - t0) * 1000.0
            statement_id = getattr(cur, "query_id", None)
        finally:
            cur.close()
        server_ms = self._fetch_server_ms(statement_id) if statement_id else None
        return len(rows), server_ms, wall_ms

    def _fetch_server_ms(self, statement_id: str) -> float | None:
        """Server-side execution time in ms, or None.

        Three tiers, because no single surface is reliable: the SQL query
        history REST API is near-instant but its response shape is not
        contractually stable; `system.query.history` is stable but can lag
        by minutes and may not be enabled. Returning None is explicitly
        permitted by the Source ABC — Benchmarker falls back to wall_ms.
        """
        for attempt in range(2):
            ms = self._server_ms_from_history_api(statement_id)
            if ms is not None:
                return ms
            if attempt == 0:
                time.sleep(0.25)
        return self._server_ms_from_system_table(statement_id)

    def _server_ms_from_history_api(self, statement_id: str) -> float | None:
        filter_by = json.dumps({"statement_ids": [statement_id]})
        query = urllib.parse.urlencode({"filter_by": filter_by})
        url = f"https://{self._host}/api/2.0/sql/history/queries?{query}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.load(response)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        for item in body.get("res") or []:
            metrics = item.get("metrics") or {}
            for key in ("execution_time_ms", "total_time_ms"):
                if metrics.get(key) is not None:
                    return float(metrics[key])
            if item.get("duration") is not None:
                return float(item["duration"])
        return None

    def _server_ms_from_system_table(self, statement_id: str) -> float | None:
        # NOTE: deviates from the brief, which interpolated statement_id
        # into the SQL string with an f-string. statement_id comes from
        # the connector (cursor.query_id), not user input, but the
        # connector supports native positional binding (`?` placeholders,
        # documented for databricks-sql-connector 3.0.0+), so we use that
        # instead of hand-rolled string interpolation.
        sql = (
            "SELECT execution_duration_ms, total_duration_ms "
            "FROM system.query.history "
            "WHERE statement_id = ? LIMIT 1"
        )
        try:
            cur = self._conn.cursor()
            try:
                cur.execute(sql, [statement_id])
                row = cur.fetchone()
            finally:
                cur.close()
        except Exception:
            return None
        if not row:
            return None
        for value in row:
            if value is not None:
                return float(value)
        return None

    def unload_to_s3(
        self,
        table: str,
        stage: "S3Stage",
        run_id: str,
        file_format: str = "parquet",
    ) -> UnloadResult:
        """Bulk-export `table` to the per-run S3 prefix with
        `INSERT OVERWRITE DIRECTORY ... USING PARQUET`.

        Requires a Unity Catalog external location over the staging bucket
        with WRITE FILES granted to this principal — the `demo` Terraform
        module provisions one. Idempotent: OVERWRITE replaces this table's
        files without touching others in the run.
        """
        from ..staging.s3 import list_s3_objects

        if file_format.lower() != "parquet":
            raise ValueError(
                f"unload_to_s3: only parquet is supported, got {file_format!r}"
            )

        target_uri = stage.s3_uri(run_id, table)
        cur = self._conn.cursor()
        try:
            t0 = time.monotonic()
            try:
                cur.execute(
                    f"INSERT OVERWRITE DIRECTORY '{target_uri}'\n"
                    f"USING PARQUET\n"
                    f"SELECT * FROM {self._fq(table)}"
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Databricks refused to write Parquet to {target_uri}. "
                    "The staging bucket needs a Unity Catalog external "
                    "location with WRITE FILES granted to this principal — "
                    "sources/databricks/terraform/demo provisions one when "
                    "enable_s3_staging=true. Until then use "
                    "Migrator.add_table() for this table; the direct path "
                    f"needs no external location. Original error: {exc}"
                ) from exc
            seconds = round(time.monotonic() - t0, 3)
        finally:
            cur.close()

        files = parquet_only(list_s3_objects(stage, run_id, table))
        return UnloadResult(
            file_count=len(files),
            total_bytes=sum(f.size for f in files),
            seconds=seconds,
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
