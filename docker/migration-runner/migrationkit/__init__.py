"""
migrationkit — Python library for AI-assisted ClickHouse migrations.

The LLM-generated migration script imports this and writes ~30 lines
that codify the migration plan. The library handles batching, per-batch
checkpointing, pause/cancel responsiveness, and structured progress
events to a shared SQLite store. The dashboard reads progress over an
HTTP API served from inside the migration-runner container.

  >>> from migrationkit import Migrator, SnowflakeSource, ClickHouseTarget, S3Stage
  >>> m = Migrator(
  ...     run_id="snowflake-tpch-2026-05-13-1430",
  ...     source=SnowflakeSource.from_env(),
  ...     target=ClickHouseTarget.from_env(),
  ... )
  >>> m.add_table("orders", source_query="SELECT * FROM orders", batch_size=100_000)
  >>> m.add_table_via_s3("lineitem", stage=S3Stage.from_env())
  >>> m.run()
"""
from .migrator import (
    Migrator,
    MigrationPaused,
    MigrationCancelled,
    PreflightError,
)
from .sources import (
    Source,
    SnowflakeSource,
    PostgresSource,
    ClickHouseOssSource,
    BigQuerySource,
    DatabricksSource,
)
from .sources.base import UnloadResult
from .targets import ClickHouseTarget
from .targets.clickhouse import LoadResult, ProgressSample
from .staging import S3Stage, GCSStage
from .state import ActiveRunError
from .validator import Validator, ValidationRow, ValidationResult
from .benchmarker import Benchmarker, BenchmarkRow, BenchmarkResult

__all__ = [
    "Migrator",
    "MigrationPaused",
    "MigrationCancelled",
    "PreflightError",
    "Source",
    "SnowflakeSource",
    "PostgresSource",
    "ClickHouseOssSource",
    "BigQuerySource",
    "DatabricksSource",
    "ClickHouseTarget",
    "S3Stage",
    "GCSStage",
    "UnloadResult",
    "LoadResult",
    "ProgressSample",
    "ActiveRunError",
    "Validator",
    "ValidationRow",
    "ValidationResult",
    "Benchmarker",
    "BenchmarkRow",
    "BenchmarkResult",
]

__version__ = "0.2.0"
