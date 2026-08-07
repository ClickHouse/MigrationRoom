from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from ..staging.s3 import S3Stage


@dataclass(frozen=True)
class UnloadResult:
    """Stats returned by `Source.unload_to_s3`. Used by the Migrator to
    populate `batches.bytes_in` + display in the dashboard."""

    file_count: int
    total_bytes: int
    seconds: float


class Source(ABC):
    """Read interface for a source database."""

    source_type: str = "unknown"
    database: str | None = None

    @abstractmethod
    def count_rows(self, query: str) -> int:
        """Return total row count for the rows produced by `query`."""

    @abstractmethod
    def iter_batches(
        self, query: str, batch_size: int
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield successive batches of row dicts. The implementation owns
        cursor lifecycle and pagination — callers just iterate."""

    @abstractmethod
    def execute_and_count(self, sql: str) -> tuple[int, float | None, float]:
        """Run `sql`, fetch every row, return `(row_count, server_ms, wall_ms)`.

        `server_ms` is the engine's own server-side execution time —
        the primary benchmark metric. Each implementation pulls it
        from the engine's native timing surface:

          - ClickHouse: `result.summary["elapsed_ns"]` (X-ClickHouse-Summary)
          - Snowflake:  `INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION()`
                        keyed on the session's `cursor.sfqid`
          - Postgres:   `EXPLAIN (ANALYZE, FORMAT JSON)` → `Execution Time`
          - BigQuery:   (planned) `jobs.get` totalSlotMs / endTime-startTime
          - Databricks: SQL query-history REST API keyed on `cursor.query_id`,
                        falling back to `system.query.history`; `None` if
                        neither is available

        `server_ms` may be `None` only when the engine genuinely can't
        return a server-side timing for this query — callers fall back
        to wall_ms in that case.

        `wall_ms` brackets execute + fetchall with `time.monotonic()`
        and is retained as a secondary diagnostic — the gap between
        server_ms and wall_ms reveals network/TLS overhead. For
        Postgres specifically wall_ms wraps the EXPLAIN ANALYZE run
        (not the bare query), so it's larger than a raw-query wall
        would have been; that's acceptable since wall_ms is no longer
        the primary number."""

    def unload_to_s3(
        self,
        table: str,
        stage: "S3Stage",
        run_id: str,
        file_format: str = "parquet",
    ) -> UnloadResult:
        """Bulk-export `table` to `s3://<bucket>/<prefix>/<run_id>/<table>/`.

        Default raises NotImplementedError; only sources that have a
        native unload (e.g. Snowflake `COPY INTO @stage`, ClickHouse OSS
        `INSERT INTO FUNCTION s3()`) override it. Migrator checks for
        availability before kicking off an `add_table_via_s3` plan."""
        raise NotImplementedError(
            f"{self.source_type} does not support S3 staging in this "
            f"version. Use Migrator.add_table() instead, or contribute "
            f"an unload_to_s3 implementation in sources/{self.source_type}.py."
        )

    def unload_to_gcs(
        self,
        table: str,
        stage,  # GCSStage — typed loosely to keep staging/gcs.py off the import path
        run_id: str,
        file_format: str = "parquet",
    ) -> UnloadResult:
        """Bulk-export `table` to `gs://<bucket>/<prefix>/<run_id>/<table>/`.

        Mirror of `unload_to_s3` for the GCS path. Default raises
        NotImplementedError; only sources that have a native unload
        to GCS (e.g. BigQuery `EXPORT DATA OPTIONS(uri='gs://...')`)
        override it. Migrator checks for availability before kicking
        off an `add_table_via_gcs` plan."""
        raise NotImplementedError(
            f"{self.source_type} does not support GCS staging in this "
            f"version. Use Migrator.add_table() instead, or contribute "
            f"an unload_to_gcs implementation in sources/{self.source_type}.py."
        )

    def close(self) -> None:
        pass
