"""
The Migrator class orchestrates a multi-table migration with per-batch
checkpointing, pause/cancel responsiveness, and structured event emission
to a shared SQLite store. The HTTP API (`migrationkit.api`) is served
separately by the migration-runner MCP process — this library only
writes to SQLite.

Typical LLM-generated migration script:

    from migrationkit import Migrator, SnowflakeSource, ClickHouseTarget, S3Stage

    m = Migrator(
        run_id="snowflake-tpch-2026-05-13-1430",
        source=SnowflakeSource.from_env(),
        target=ClickHouseTarget.from_env(),
    )
    # Direct path for small / medium tables
    m.add_table("orders",
                source_query="SELECT * FROM orders",
                target_table="orders",
                batch_size=100_000)
    # S3-staged path for large tables (Snowflake + ClickHouse OSS today)
    m.add_table_via_s3("lineitem", stage=S3Stage.from_env())
    m.run()
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Union

from . import state
from .sources.base import Source
from .staging.s3 import S3Stage, delete_s3_prefix
from .staging.gcs import GCSStage, delete_gcs_prefix
from .targets.clickhouse import ClickHouseTarget, ProgressSample

RowTransform = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class _TablePlan:
    name: str
    source_query: str
    target_table: str
    batch_size: int = 100_000
    transform: RowTransform | None = None
    strategy: str = "direct"
    # Populated by Migrator._preflight() — {lower(col): actual_col} for
    # the target table, queried once before any data moves.
    case_map: dict[str, str] | None = None


@dataclass
class _S3TablePlan:
    name: str
    target_table: str
    stage: S3Stage
    source_query: str | None = None        # default: SELECT * FROM <name>
    file_format: str = "parquet"
    cleanup_staged: bool = True
    strategy: str = "s3_stage"
    # See _TablePlan.case_map. Unused by the S3 path (INSERT FROM s3()
    # delegates column matching to ClickHouse), but populated by
    # preflight so the existence check still fires.
    case_map: dict[str, str] | None = None


@dataclass
class _GCSTablePlan:
    """Mirror of _S3TablePlan for the BigQuery → GCS → ClickHouse Cloud
    staging path. The phase machine (unloading → staged → loading →
    validating) is identical; only the table-function name and
    credential type differ."""

    name: str
    target_table: str
    stage: GCSStage
    source_query: str | None = None
    file_format: str = "parquet"
    cleanup_staged: bool = True
    strategy: str = "gcs_stage"
    case_map: dict[str, str] | None = None


_AnyPlan = Union[_TablePlan, _S3TablePlan, _GCSTablePlan]


class MigrationPaused(RuntimeError):
    pass


class MigrationCancelled(RuntimeError):
    pass


class PreflightError(RuntimeError):
    """Raised by Migrator._preflight() when a plan can't be reconciled
    against the live target schema (table missing, no introspectable
    columns, etc.). Failing here means failing BEFORE any rows have
    moved — partners get an actionable error instead of a half-migrated
    state to clean up."""
    pass


def _validate_target_table_name(name: str) -> None:
    """Reject anything that isn't a bare table identifier. The Migrator
    owns the database (`target_database` on the constructor); the table
    name must be just the table — no `db.table`, no quoting, no
    whitespace.

    Catches the most common agent-generated mistake: passing
    `db.table` to `target_table=...` after copy-pasting from a
    CREATE TABLE statement. Without this check, `db.table` ends up
    literal in `system.columns WHERE table = 'db.table'` (which finds
    nothing) and a downstream `client.insert("db.table", …)` either
    misroutes or fails with a cryptic column-name error mid-batch."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("target_table must be a non-empty string")
    if "." in name:
        raise ValueError(
            f"target_table {name!r} is database-qualified. Pass just the "
            f"table name; the Migrator already knows the target database "
            f"(set via Migrator(target_database=...) or "
            f"ClickHouseTarget.from_env())."
        )
    for ch in ("`", '"', "'", " ", "\t", "\n"):
        if ch in name:
            raise ValueError(
                f"target_table {name!r} contains an illegal character "
                f"({ch!r}). Pass a plain identifier."
            )


class Migrator:
    """One Migrator per migration run. Refuses construction if another run
    is already 'running' or 'paused' in the shared state store."""

    def __init__(
        self,
        run_id: str,
        source: Source,
        target: ClickHouseTarget,
        *,
        target_database: str | None = None,
        pause_poll_seconds: float = 1.0,
    ) -> None:
        self.run_id = run_id
        self.source = source
        self.target = target
        # Authoritative target database name for this run. If the caller
        # didn't pass it explicitly, fall back to whatever the
        # ClickHouseTarget was constructed with (CLICKHOUSE_CLOUD_DATABASE
        # by default). Either way, this is the ONE name we use for
        # schema introspection in _preflight() and for inserts at run
        # time — there is no second source of truth (no `db.table`
        # qualified names allowed on add_table).
        self.target_database = target_database or target.database
        if not self.target_database:
            raise ValueError(
                "Migrator: target_database is required. Pass it explicitly "
                "or set CLICKHOUSE_CLOUD_DATABASE before constructing "
                "ClickHouseTarget.from_env()."
            )
        # Propagate to the target so every operation — `Client.insert`,
        # `Client.query`, `INSERT INTO {self.database}.{table}` — resolves
        # against the run's database. Without this, bare-name batch
        # inserts silently land in CLICKHOUSE_CLOUD_DATABASE.
        self.target.use_database(self.target_database)
        self._plans: list[_AnyPlan] = []
        self._pause_poll_seconds = pause_poll_seconds
        # create_run raises ActiveRunError if a concurrent run is active
        state.create_run(
            run_id=run_id,
            source_type=source.source_type,
            source_database=getattr(source, "database", None),
            target_database=self.target_database,
        )

    # ── plan registration ────────────────────────────────────────────

    def add_table(
        self,
        name: str,
        *,
        source_query: str,
        target_table: str | None = None,
        batch_size: int = 100_000,
        transform: RowTransform | None = None,
    ) -> "Migrator":
        resolved_target = target_table or name
        _validate_target_table_name(resolved_target)
        plan = _TablePlan(
            name=name,
            source_query=source_query,
            target_table=resolved_target,
            batch_size=batch_size,
            transform=transform,
        )
        self._plans.append(plan)
        return self

    def add_table_via_gcs(
        self,
        name: str,
        *,
        stage: GCSStage,
        target_table: str | None = None,
        source_query: str | None = None,
        file_format: str = "parquet",
        cleanup_staged: bool = True,
    ) -> "Migrator":
        """Register a table for GCS-staged migration: source unloads to
        GCS, then ClickHouse Cloud loads via `INSERT FROM gcs(...)`.

        Only sources that override `Source.unload_to_gcs()` (BigQuery
        in v1) support this path — Migrator validates at `run()` time."""
        resolved_target = target_table or name
        _validate_target_table_name(resolved_target)
        plan = _GCSTablePlan(
            name=name,
            target_table=resolved_target,
            stage=stage,
            source_query=source_query,
            file_format=file_format,
            cleanup_staged=cleanup_staged,
        )
        self._plans.append(plan)
        return self

    def add_table_via_s3(
        self,
        name: str,
        *,
        stage: S3Stage,
        target_table: str | None = None,
        source_query: str | None = None,
        file_format: str = "parquet",
        cleanup_staged: bool = True,
    ) -> "Migrator":
        """Register a table for S3-staged migration: source unloads to
        S3, then ClickHouse Cloud loads via `INSERT FROM s3(...)`.

        Only sources that override `Source.unload_to_s3()` (Snowflake,
        ClickHouse OSS, and Databricks today) support this path —
        Migrator validates at `run()` time."""
        resolved_target = target_table or name
        _validate_target_table_name(resolved_target)
        plan = _S3TablePlan(
            name=name,
            target_table=resolved_target,
            stage=stage,
            source_query=source_query,
            file_format=file_format,
            cleanup_staged=cleanup_staged,
        )
        self._plans.append(plan)
        return self

    # ── execution ────────────────────────────────────────────────────

    def run(self) -> None:
        """Block until all tables are migrated, or the run is cancelled.
        Pause is handled in-process: the migrator blocks at the next
        batch boundary (direct path) or phase boundary (s3 path) until
        the dashboard clears the pause flag.

        On cancel, raises MigrationCancelled. On uncaught error, marks
        the run failed and re-raises."""
        try:
            # 0. Preflight: validate every plan against the live target
            #    schema BEFORE any data moves. Catches missing tables,
            #    typos, and column-case mismatches at the front of the
            #    run rather than mid-batch on table 5 of 8.
            self._preflight()

            # Pre-register all tables so the dashboard sees the full
            # plan immediately. For staged plans (S3 / GCS), we still
            # count rows up front (the source's COUNT(*) is cheap) so
            # the progress bar has a denominator from the start.
            for p in self._plans:
                if isinstance(p, (_S3TablePlan, _GCSTablePlan)):
                    source_query = p.source_query or f"SELECT * FROM {p.name}"
                else:
                    source_query = p.source_query
                try:
                    total = self.source.count_rows(source_query)
                except Exception as e:
                    self._log(f"WARN: count_rows failed for {p.name}: {e}")
                    total = None
                state.register_table(self.run_id, p.name, total, p.strategy)

            for p in self._plans:
                if isinstance(p, _S3TablePlan):
                    self._migrate_table_via_s3(p)
                elif isinstance(p, _GCSTablePlan):
                    self._migrate_table_via_gcs(p)
                else:
                    self._migrate_table(p)

            state.finish_run(self.run_id, "done")
            self._log(f"✅ Migration {self.run_id} complete.")
        except MigrationCancelled:
            state.finish_run(self.run_id, "cancelled")
            self._log("✖  Migration cancelled.")
            raise
        except Exception as e:
            err = "".join(traceback.format_exception_only(type(e), e)).strip()
            state.finish_run(self.run_id, "failed", error=err)
            self._log(f"✗ Migration failed: {err}")
            raise
        finally:
            self.source.close()
            self.target.close()

    # ── preflight ────────────────────────────────────────────────────

    def _preflight(self) -> None:
        """Introspect every plan's target table BEFORE a single row
        moves. For each plan, query the live ClickHouse schema for
        `(self.target_database, plan.target_table)` and cache the
        column case-map on the plan. Two things this buys us:

        1. **Fail-fast on missing or misnamed targets.** If a partner
           skipped step 1, or the agent typo'd a target_table name,
           we raise `PreflightError` here — with the bad name in the
           message — instead of after migrating 4 of 8 tables.
        2. **No per-batch schema queries.** The hot insert path
           (`ClickHouseTarget.insert_batch`) reads the cached case-map
           verbatim. No more "trust then catch" pattern where each batch
           is its own potential explosion site for column-case races.

        Plans are reconciled against ONE database (self.target_database).
        Qualified `target_table` names are rejected at registration
        time by `_validate_target_table_name`, so we never end up
        introspecting against a partner's typo'd second database here."""
        if not self._plans:
            return
        self._log(
            f"▶ Pre-flight: checking {len(self._plans)} target table(s) "
            f"in `{self.target_database}` …"
        )
        for p in self._plans:
            case_map = self.target.introspect_columns(
                self.target_database, p.target_table
            )
            if case_map is None:
                raise PreflightError(
                    f"Target table `{self.target_database}.{p.target_table}` "
                    f"does not exist (or is not introspectable). Run step 1 "
                    f"again to create it, then retry step 2."
                )
            if not case_map:
                raise PreflightError(
                    f"Target table `{self.target_database}.{p.target_table}` "
                    f"has no columns visible in system.columns. Did the "
                    f"CREATE TABLE in step 1 succeed?"
                )
            p.case_map = case_map
            self._log(
                f"  ✓ {p.target_table}: {len(case_map)} column(s)"
            )

    # ── direct path (unchanged) ──────────────────────────────────────

    def _migrate_table(self, plan: _TablePlan) -> None:
        state.set_table_status(self.run_id, plan.name, "running")
        self._log(f"▶ {plan.name}: starting")
        batch_n = 0
        offset = 0
        # Mark the table failed before re-raising, the way both staged paths
        # do. Without this the run row goes to "failed" while the table row
        # stays "running", and the dashboard reports a live migration at
        # 0 rows/sec forever — which reads as a hang rather than the error it
        # is. A transform raising on the first row (the common case) produced
        # exactly that: "0 done · 1 running · 7 pending" with nothing moving.
        # MigrationPaused/MigrationCancelled deliberately propagate untouched;
        # they are control flow, not failure, and the run-level handler owns
        # the resulting status.
        try:
            for batch in self.source.iter_batches(plan.source_query, plan.batch_size):
                self._check_controls()
                if plan.transform:
                    batch = [plan.transform(row) for row in batch]
                t0 = time.monotonic()
                inserted = self.target.insert_batch(
                    plan.target_table, batch, case_map=plan.case_map
                )
                elapsed = round(time.monotonic() - t0, 3)
                state.record_batch(
                    run_id=self.run_id,
                    table_name=plan.name,
                    batch_n=batch_n,
                    rows=inserted,
                    source_offset=offset,
                    bytes_in=None,
                    bytes_out=None,
                    seconds=elapsed,
                )
                offset += inserted
                batch_n += 1
                self._log(
                    f"  {plan.name} batch {batch_n}: {inserted} rows in {elapsed}s "
                    f"(total {offset})"
                )
        except (MigrationPaused, MigrationCancelled):
            raise
        except Exception as e:
            state.set_table_status(self.run_id, plan.name, "failed")
            state.write_event(self.run_id, "table_failed",
                              {"table": plan.name, "error": str(e),
                               "rows_done": offset, "strategy": "direct"})
            self._log(f"✗ {plan.name}: failed after {offset} rows — {e}")
            raise
        state.set_table_status(self.run_id, plan.name, "done")
        state.write_event(self.run_id, "table_done",
                          {"table": plan.name, "total_rows": offset, "strategy": "direct"})
        self._log(f"✓ {plan.name}: done ({offset} rows)")

    # ── S3 staging path ──────────────────────────────────────────────

    def _migrate_table_via_s3(self, plan: _S3TablePlan) -> None:
        """Phase machine: unloading → staged → loading → validating → done.
        Each transition writes a `phase_started` event so the dashboard
        renders the 4-segment phase indicator + milestones correctly."""
        # Verify the source supports unload — fail fast if a partner
        # accidentally calls add_table_via_s3 on a source that doesn't
        # implement it.
        if type(self.source).unload_to_s3 is Source.unload_to_s3:
            raise NotImplementedError(
                f"add_table_via_s3 requires a source that implements "
                f"unload_to_s3; {self.source.source_type!r} does not."
            )

        state.set_table_status(self.run_id, plan.name, "running")
        self._log(f"▶ {plan.name}: starting (S3 staged)")
        t_total = time.monotonic()

        # ── Phase 1: unload to S3 ─────────────────────────────────
        self._check_controls()
        self._enter_phase(plan.name, "unloading")
        try:
            unload = self.source.unload_to_s3(
                table=plan.name,
                stage=plan.stage,
                run_id=self.run_id,
                file_format=plan.file_format,
            )
        except Exception as e:
            state.set_table_status(self.run_id, plan.name, "failed")
            state.write_event(self.run_id, "log", {
                "message": f"✗ {plan.name}: unload failed — {e}"
            })
            raise

        # ── Phase 2: staged (intermediate marker) ─────────────────
        self._enter_phase(
            plan.name, "staged",
            extra={"file_count": unload.file_count, "total_bytes": unload.total_bytes},
        )
        self._log(
            f"  {plan.name}: staged {_fmt_bytes(unload.total_bytes)} "
            f"in {unload.file_count} file(s), {unload.seconds}s"
        )

        # ── Phase 3: load into ClickHouse Cloud ───────────────────
        self._check_controls()
        self._enter_phase(
            plan.name, "loading",
            extra={"total_bytes": unload.total_bytes},
        )
        s3_glob = plan.stage.s3_glob(self.run_id, plan.name, plan.file_format)

        def _on_progress(s: ProgressSample) -> None:
            state.write_event(self.run_id, "bytes_progress", {
                "table": plan.name,
                "bytes_done": s.bytes_done,
                "total_bytes": unload.total_bytes,
                "rows_done": s.rows_done,
            })

        try:
            load = self.target.load_from_s3(
                target_table=plan.target_table,
                s3_glob=s3_glob,
                stage=plan.stage,
                file_format=_clickhouse_format(plan.file_format),
                on_progress=_on_progress,
            )
        except Exception as e:
            state.set_table_status(self.run_id, plan.name, "failed")
            state.write_event(self.run_id, "log", {
                "message": f"✗ {plan.name}: load failed — {e}"
            })
            raise

        # ── Phase 4: validate row counts ──────────────────────────
        self._check_controls()
        self._enter_phase(plan.name, "validating")
        # Source row count was captured at registration; if the count is
        # unknown there, fall back to a fresh count.
        run = state.get_run(self.run_id) or {}
        registered_total = None
        for t in run.get("tables", []):
            if t["table_name"] == plan.name:
                registered_total = t.get("total_rows")
                break
        if registered_total in (None, 0):
            registered_total = self.source.count_rows(
                plan.source_query or f"SELECT * FROM {plan.name}"
            )
        if load.rows_done != registered_total:
            state.set_table_status(self.run_id, plan.name, "failed")
            msg = (
                f"row count mismatch for {plan.name}: "
                f"source={registered_total}, target={load.rows_done}"
            )
            state.write_event(self.run_id, "log", {"message": f"✗ {msg}"})
            raise RuntimeError(msg)

        # ── Cleanup + done ────────────────────────────────────────
        if plan.cleanup_staged:
            try:
                n_deleted = delete_s3_prefix(plan.stage, self.run_id, plan.name)
                self._log(f"  {plan.name}: cleaned up {n_deleted} S3 object(s)")
            except Exception as e:
                # Cleanup failure shouldn't fail the migration — just log.
                self._log(f"  {plan.name}: WARN cleanup failed — {e}")

        total_seconds = round(time.monotonic() - t_total, 3)
        state.record_staged_load(
            run_id=self.run_id,
            table_name=plan.name,
            rows=load.rows_done,
            bytes_in=unload.total_bytes,
            bytes_out=load.bytes_read,
            seconds=total_seconds,
        )
        state.set_table_phase(self.run_id, plan.name, None)
        state.set_table_status(self.run_id, plan.name, "done")
        state.write_event(self.run_id, "table_done", {
            "table": plan.name,
            "total_rows": load.rows_done,
            "total_bytes": unload.total_bytes,
            "file_count": unload.file_count,
            "strategy": "s3_stage",
            "duration_seconds": total_seconds,
        })
        self._log(f"✓ {plan.name}: done ({load.rows_done} rows · {_fmt_bytes(unload.total_bytes)} · {total_seconds}s)")

    # ── GCS staging path (mirrors S3) ────────────────────────────────

    def _migrate_table_via_gcs(self, plan: _GCSTablePlan) -> None:
        """BigQuery → GCS → ClickHouse Cloud. Phase machine identical
        to `_migrate_table_via_s3` — the only differences are the
        unload/load helpers and the credentials. Kept as a parallel
        method (rather than a generic abstraction) so the S3 path stays
        as easy to read; the duplication is bounded and the two
        engines have just enough differences (URI scheme, glob format,
        credential type) that a shared adapter would mostly forward
        through three method calls anyway."""
        if type(self.source).unload_to_gcs is Source.unload_to_gcs:
            raise NotImplementedError(
                f"add_table_via_gcs requires a source that implements "
                f"unload_to_gcs; {self.source.source_type!r} does not."
            )

        state.set_table_status(self.run_id, plan.name, "running")
        self._log(f"▶ {plan.name}: starting (GCS staged)")
        t_total = time.monotonic()

        # ── Phase 1: unload to GCS ────────────────────────────────
        self._check_controls()
        self._enter_phase(plan.name, "unloading")
        try:
            unload = self.source.unload_to_gcs(
                table=plan.name,
                stage=plan.stage,
                run_id=self.run_id,
                file_format=plan.file_format,
            )
        except Exception as e:
            state.set_table_status(self.run_id, plan.name, "failed")
            state.write_event(self.run_id, "log", {
                "message": f"✗ {plan.name}: unload failed — {e}"
            })
            raise

        # ── Phase 2: staged ───────────────────────────────────────
        self._enter_phase(
            plan.name, "staged",
            extra={"file_count": unload.file_count, "total_bytes": unload.total_bytes},
        )
        self._log(
            f"  {plan.name}: staged {_fmt_bytes(unload.total_bytes)} "
            f"in {unload.file_count} file(s), {unload.seconds}s"
        )

        # ── Phase 3: load into ClickHouse Cloud ───────────────────
        self._check_controls()
        self._enter_phase(
            plan.name, "loading",
            extra={"total_bytes": unload.total_bytes},
        )
        gcs_glob = plan.stage.gcs_glob(self.run_id, plan.name, plan.file_format)

        def _on_progress(s: ProgressSample) -> None:
            state.write_event(self.run_id, "bytes_progress", {
                "table": plan.name,
                "bytes_done": s.bytes_done,
                "total_bytes": unload.total_bytes,
                "rows_done": s.rows_done,
            })

        try:
            load = self.target.load_from_gcs(
                target_table=plan.target_table,
                gcs_glob=gcs_glob,
                stage=plan.stage,
                file_format=_clickhouse_format(plan.file_format),
                on_progress=_on_progress,
            )
        except Exception as e:
            state.set_table_status(self.run_id, plan.name, "failed")
            state.write_event(self.run_id, "log", {
                "message": f"✗ {plan.name}: load failed — {e}"
            })
            raise

        # ── Phase 4: validate row counts ──────────────────────────
        self._check_controls()
        self._enter_phase(plan.name, "validating")
        run = state.get_run(self.run_id) or {}
        registered_total = None
        for t in run.get("tables", []):
            if t["table_name"] == plan.name:
                registered_total = t.get("total_rows")
                break
        if registered_total in (None, 0):
            registered_total = self.source.count_rows(
                plan.source_query or f"SELECT * FROM {plan.name}"
            )
        if load.rows_done != registered_total:
            state.set_table_status(self.run_id, plan.name, "failed")
            msg = (
                f"row count mismatch for {plan.name}: "
                f"source={registered_total}, target={load.rows_done}"
            )
            state.write_event(self.run_id, "log", {"message": f"✗ {msg}"})
            raise RuntimeError(msg)

        # ── Cleanup + done ────────────────────────────────────────
        if plan.cleanup_staged:
            try:
                n_deleted = delete_gcs_prefix(plan.stage, self.run_id, plan.name)
                self._log(f"  {plan.name}: cleaned up {n_deleted} GCS object(s)")
            except Exception as e:
                self._log(f"  {plan.name}: WARN cleanup failed — {e}")

        total_seconds = round(time.monotonic() - t_total, 3)
        state.record_staged_load(
            run_id=self.run_id,
            table_name=plan.name,
            rows=load.rows_done,
            bytes_in=unload.total_bytes,
            bytes_out=load.bytes_read,
            seconds=total_seconds,
        )
        state.set_table_phase(self.run_id, plan.name, None)
        state.set_table_status(self.run_id, plan.name, "done")
        state.write_event(self.run_id, "table_done", {
            "table": plan.name,
            "total_rows": load.rows_done,
            "total_bytes": unload.total_bytes,
            "file_count": unload.file_count,
            "strategy": "gcs_stage",
            "duration_seconds": total_seconds,
        })
        self._log(f"✓ {plan.name}: done ({load.rows_done} rows · {_fmt_bytes(unload.total_bytes)} · {total_seconds}s)")

    def _enter_phase(
        self,
        table: str,
        phase: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Transition a staged table to a new phase: writes the phase
        column AND emits a `phase_started` event (dashboard reads both)."""
        state.set_table_phase(self.run_id, table, phase)
        payload: dict[str, Any] = {"table": table, "phase": phase}
        if extra:
            payload.update(extra)
        state.write_event(self.run_id, "phase_started", payload)

    # ── controls ─────────────────────────────────────────────────────

    def _check_controls(self) -> None:
        """Called between batches (direct) or phase boundaries (s3).
        Cancel raises; pause blocks in place until the dashboard clears
        the flag (so resume is just: poll loop notices the flag flipped
        to 'run' and returns)."""
        flag = state.get_control_flag(self.run_id)
        if flag == "cancel":
            state.write_event(self.run_id, "cancelled", {})
            raise MigrationCancelled()
        if flag != "pause":
            return

        # Pause: mark run paused, emit one event, block.
        state.set_run_status(self.run_id, "paused")
        state.write_event(self.run_id, "paused", {})
        self._log("⏸  Migration paused at boundary; awaiting resume…")
        while True:
            time.sleep(self._pause_poll_seconds)
            flag = state.get_control_flag(self.run_id)
            if flag == "cancel":
                state.write_event(self.run_id, "cancelled", {})
                raise MigrationCancelled()
            if flag == "run":
                state.set_run_status(self.run_id, "running")
                state.write_event(self.run_id, "resumed", {})
                self._log("▶  Resumed.")
                return

    # ── logging helper ───────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        # Print to stdout so tail_python_job surfaces it AND write a log
        # event so the dashboard can show the same trail.
        print(msg, flush=True)
        state.write_event(self.run_id, "log", {"message": msg})

    # convenience for tests / debugging
    @staticmethod
    def emit_log(run_id: str, message: str) -> None:
        state.write_event(run_id, "log", {"message": message})


# ── helpers ────────────────────────────────────────────────────────────


def _clickhouse_format(file_format: str) -> str:
    """Map our lower-case format name to ClickHouse's CamelCase."""
    return {"parquet": "Parquet", "csv": "CSV"}.get(file_format.lower(), file_format)


def _fmt_bytes(n: int) -> str:
    """Human-readable bytes, used only for log lines (UI does its own)."""
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GiB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.2f} MiB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KiB"
    return f"{n} B"
