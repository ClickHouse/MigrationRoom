from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ..staging.s3 import S3Stage


@dataclass(frozen=True)
class ProgressSample:
    """One in-flight reading from system.processes during INSERT FROM s3().
    bytes_done / rows_done are cumulative; elapsed_ms is the server-side
    time-since-query-started for the running INSERT."""

    bytes_done: int
    rows_done: int
    elapsed_ms: int


@dataclass(frozen=True)
class LoadResult:
    """Stats returned by `ClickHouseTarget.load_from_s3`. Populates
    `batches.bytes_out` + final row count for validation."""

    rows_done: int
    bytes_read: int
    seconds: float


class ClickHouseTarget:
    """Write target = ClickHouse Cloud (or any HTTPS-capable ClickHouse)."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        secure: bool = True,
    ) -> None:
        import clickhouse_connect

        self.database = database
        # Hang on to connection params so load_from_s3 can open a SECOND
        # client (the polling connection) without sharing the long-lived
        # one — clickhouse-connect is not thread-safe per client.
        self._conn_params = dict(
            host=host,
            port=port,
            username=user,
            password=password,
            database=database,
            secure=secure,
        )
        self._client = clickhouse_connect.get_client(**self._conn_params)

    def use_database(self, database: str) -> None:
        """Route every subsequent operation through `database`.

        Updates `self.database` (used by SQL we format ourselves, e.g.
        `INSERT INTO {self.database}.{table}` in load_from_s3 / load_from_gcs
        and `count_rows`), AND the underlying clickhouse-connect client's
        `database` attribute (used by client-level helpers like
        `Client.query(sql)` and `Client.insert(table, ...)` when an
        unqualified table name resolves against the session default).
        Also threads through `_conn_params` so any future polling-client
        opened by load_from_s3 / load_from_gcs inherits the same default.

        Called by Migrator / Validator / Benchmarker constructors after
        resolving the run's target_database — without this, a bare-table
        INSERT silently lands in CLICKHOUSE_CLOUD_DATABASE."""
        self.database = database
        self._conn_params["database"] = database
        # clickhouse-connect's Client.database is a settable attribute
        # that's sent on every HTTP request. No reconnect needed.
        self._client.database = database

    @classmethod
    def from_env(cls) -> "ClickHouseTarget":
        host = os.environ["CLICKHOUSE_CLOUD_HOST"]
        # ClickHouse Cloud HTTPS is 8443; OSS HTTP is 8123. Default to 8443.
        port = int(os.environ.get("CLICKHOUSE_CLOUD_PORT", "8443"))
        return cls(
            host=host,
            port=port,
            user=os.environ.get("CLICKHOUSE_CLOUD_USER", "default"),
            password=os.environ.get("CLICKHOUSE_CLOUD_PASSWORD", ""),
            database=os.environ.get("CLICKHOUSE_CLOUD_DATABASE", "default"),
            secure=os.environ.get("CLICKHOUSE_CLOUD_SECURE", "true").lower() != "false",
        )

    def count_rows(self, table: str) -> int:
        result = self._client.query(f"SELECT count() FROM {self.database}.{table}")
        return int(result.first_row[0])

    def execute_and_count(self, sql: str) -> tuple[int, float | None, float]:
        """Run `sql`, fetch every row, return `(row_count, server_ms, wall_ms)`.
        Mirrors `Source.execute_and_count` on the source side so
        `Benchmarker` can call the same shape on both connections.
        `server_ms` is pulled from `X-ClickHouse-Summary` (network-neutral);
        `wall_ms` brackets execute + result transfer."""
        import time
        t0 = time.monotonic()
        result = self._client.query(sql)
        rows = result.result_rows  # forces full fetch
        wall_ms = (time.monotonic() - t0) * 1000.0
        elapsed_ns = result.summary.get("elapsed_ns") if result.summary else None
        server_ms = float(elapsed_ns) / 1e6 if elapsed_ns else None
        return len(rows), server_ms, wall_ms

    def introspect_columns(
        self, database: str, table: str
    ) -> dict[str, str] | None:
        """Return {lower(col): actual_col} for `database`.`table`, or
        `None` if the table can't be introspected (doesn't exist, no
        permissions on system.columns, etc.).

        Called exactly once per plan by `Migrator._preflight()` —
        before any rows move. The returned case-map is stashed on the
        plan and threaded into every subsequent `insert_batch` call,
        which means the hot insert path never queries system.columns
        itself. This eliminates an entire class of mid-migration
        failures (schema cache races, qualified-name miss, partial
        DDL visibility) at their structural root: the contract is
        "validate up front, fail loudly", not "trust then catch".

        ALIAS and MATERIALIZED columns are excluded, because ClickHouse
        computes them and rejects an INSERT that supplies them. Leaving them
        in the map made every source-side generated column a landmine: the
        row arrives carrying `o_orderyear`, the map matches it to the target's
        ALIAS column, and the insert fails. Excluding them here means
        `insert_batch`'s existing "no matching target column → drop" rule
        handles the value, which is the correct outcome for both kinds — the
        target recomputes an ALIAS on read and a MATERIALIZED on write, so
        the source's copy is redundant either way.

        This matters because the migration instructions actively steer the
        agent toward `MATERIALIZED`/`ALIAS` for generated columns. Without
        this filter, taking that advice forces every generated column to be
        hand-deleted in a `transform=`, which is exactly the boilerplate this
        framework exists to remove. DEFAULT columns are kept: they are
        insertable, and the default only applies when a value is absent."""
        try:
            rows = self._client.query(
                "SELECT name FROM system.columns "
                "WHERE database = {db:String} AND table = {tbl:String} "
                "  AND default_kind NOT IN ('ALIAS', 'MATERIALIZED') "
                "ORDER BY position",
                parameters={"db": database, "tbl": table},
            ).result_rows
        except Exception:
            return None
        if not rows:
            return None
        return {str(r[0]).lower(): str(r[0]) for r in rows}

    def insert_batch(
        self,
        table: str,
        rows: list[dict[str, Any]],
        *,
        case_map: dict[str, str] | None = None,
    ) -> int:
        """Insert a batch of row-dicts into `table` (bare name; the
        call passes `database=self.database` to clickhouse-connect so
        the target's current database — set by `use_database()` from
        Migrator — qualifies the table).

        `case_map` is REQUIRED: `{lower(col): actual_col}` for the
        target table, supplied by `Migrator._preflight()`. Row-dict
        keys are case-folded and looked up in the map, so the agent's
        `transform=` lambda can use either case freely. Keys that
        don't match any target column are dropped from the insert
        (friendlier than failing a 100k-row batch on a stray key
        introduced by `**row` in a lambda).

        We deliberately do NOT introspect schema here. Calling
        `insert_batch` without `case_map` raises rather than silently
        falling back — a missing case_map means the Migrator skipped
        preflight, which is a bug we want surfaced loudly, not buried
        in a verbatim insert that may or may not happen to succeed."""
        if not rows:
            return 0
        if not case_map:
            raise RuntimeError(
                f"insert_batch({table!r}): case_map is required. "
                f"The Migrator must run _preflight() before any "
                f"_migrate_table() call so each plan carries its "
                f"target column case-map. Got case_map={case_map!r}."
            )

        normalized: list[dict[str, Any]] = []
        for r in rows:
            collapsed: dict[str, Any] = {}
            for k, v in r.items():
                target_name = case_map.get(k.lower())
                if target_name is None:
                    continue  # row key has no matching target column → drop
                collapsed[target_name] = v
            normalized.append(collapsed)

        if not normalized[0]:
            raise RuntimeError(
                f"insert_batch({table!r}): no row keys matched any "
                f"column on the target. Row keys = {sorted(rows[0].keys())}; "
                f"target columns = {sorted(case_map.values())}."
            )

        column_names = list(normalized[0].keys())
        data = [[r.get(c) for c in column_names] for r in normalized]
        # Pass database= explicitly. clickhouse-connect's Client.insert
        # otherwise resolves a bare table name against the database the
        # client was constructed with — which is CLICKHOUSE_CLOUD_DATABASE
        # from env, NOT `self.database` after Migrator/Validator override.
        self._client.insert(
            table, data, column_names=column_names, database=self.database
        )
        return len(rows)

    def load_from_s3(
        self,
        target_table: str,
        s3_glob: str,
        stage: "S3Stage",
        file_format: str = "Parquet",
        on_progress: Callable[[ProgressSample], None] | None = None,
        poll_interval_seconds: float = 2.0,
    ) -> LoadResult:
        """`INSERT INTO target SELECT * FROM s3(...)`. Submitted in a
        worker thread so the main thread can poll `system.processes`
        for in-flight progress samples.

        `on_progress` is called every ~poll_interval_seconds with a
        `ProgressSample`. If the poll query returns no rows (because
        the user lacks visibility into system.processes, or the INSERT
        is queued not running), progress reporting gracefully no-ops
        and the load still completes."""
        import clickhouse_connect

        query_id = str(uuid.uuid4())
        # The s3() table function uses positional args: url, access_key,
        # secret_key, format. Snowflake-unloaded Parquet → ClickHouse
        # is the only path we exercise in v1.
        sql = (
            f"INSERT INTO {self.database}.{target_table} "
            f"SELECT * FROM s3("
            f"'{s3_glob}', "
            f"'{stage.access_key_id}', "
            f"'{stage.secret_access_key}', "
            f"'{file_format}'"
            f")"
        )

        # Worker: runs the INSERT and stashes outcome/exception in a box.
        outcome: dict[str, Any] = {"error": None, "summary": None}

        def _worker() -> None:
            try:
                # `query_id` setting lets us find the running INSERT in
                # system.processes. clickhouse-connect's `settings` kwarg
                # forwards to the HTTP query string.
                self._client.command(sql, settings={"query_id": query_id})
            except Exception as e:
                outcome["error"] = e

        # Baseline before the INSERT so `rows_done` reflects rows
        # added by THIS load (not total target rows, which over-counts
        # on re-runs into a non-empty table).
        try:
            baseline_rows = self.count_rows(target_table)
        except Exception:
            baseline_rows = 0

        t0 = time.monotonic()
        thread = threading.Thread(target=_worker, name=f"mk-load-{query_id[:8]}", daemon=True)
        thread.start()

        # Polling connection — separate client because clickhouse-connect
        # isn't thread-safe to share.
        poll_client = clickhouse_connect.get_client(**self._conn_params)
        last_sample = ProgressSample(0, 0, 0)
        try:
            while thread.is_alive():
                time.sleep(poll_interval_seconds)
                try:
                    # clusterAllReplicas covers ClickHouse Cloud's
                    # multi-replica setup; falls back to local on OSS.
                    rows = poll_client.query(
                        "SELECT read_bytes, total_rows_approx, elapsed "
                        "FROM clusterAllReplicas('default', system.processes) "
                        f"WHERE query_id = '{query_id}' "
                        "ORDER BY elapsed DESC LIMIT 1"
                    ).result_rows
                except Exception:
                    rows = []
                if rows:
                    rb, tra, elapsed = rows[0]
                    sample = ProgressSample(
                        bytes_done=int(rb or 0),
                        rows_done=int(tra or 0),
                        elapsed_ms=int(float(elapsed or 0) * 1000),
                    )
                    last_sample = sample
                    if on_progress:
                        try:
                            on_progress(sample)
                        except Exception:
                            # progress reporting must never crash the load
                            pass
        finally:
            try:
                poll_client.close()
            except Exception:
                pass

        thread.join()
        seconds = round(time.monotonic() - t0, 3)
        if outcome["error"] is not None:
            raise outcome["error"]

        # Final row count is the source of truth; system.processes drops
        # the row once the query finishes, so last_sample.rows_done is a
        # mid-flight estimate, not the ground truth.
        final_rows = max(0, self.count_rows(target_table) - baseline_rows)
        return LoadResult(
            rows_done=final_rows,
            bytes_read=last_sample.bytes_done,
            seconds=seconds,
        )

    def load_from_gcs(
        self,
        target_table: str,
        gcs_glob: str,
        stage,  # GCSStage — kept loose to avoid pulling staging/gcs.py into the type graph
        file_format: str = "Parquet",
        on_progress: Callable[[ProgressSample], None] | None = None,
        poll_interval_seconds: float = 2.0,
    ) -> LoadResult:
        """`INSERT INTO target SELECT * FROM gcs(...)`. Same thread +
        polling pattern as `load_from_s3` — the only differences are
        the table function name (`gcs` vs `s3`) and the credentials
        (HMAC key/secret on the stage). Progress polling, baseline
        counting, and final row-count fallback all behave identically."""
        import clickhouse_connect

        query_id = str(uuid.uuid4())
        # ClickHouse's gcs() function signature mirrors s3()'s:
        # gcs(url, hmac_key, hmac_secret, format). HMAC creds live on
        # the stage — they're a separate auth path from the SA JSON
        # used for the upload side.
        sql = (
            f"INSERT INTO {self.database}.{target_table} "
            f"SELECT * FROM gcs("
            f"'{gcs_glob}', "
            f"'{stage.hmac_access_key_id}', "
            f"'{stage.hmac_secret_access_key}', "
            f"'{file_format}'"
            f")"
        )

        outcome: dict[str, Any] = {"error": None, "summary": None}

        def _worker() -> None:
            try:
                self._client.command(sql, settings={"query_id": query_id})
            except Exception as e:
                outcome["error"] = e

        # Baseline before INSERT so rows_done counts only THIS load.
        try:
            baseline_rows = self.count_rows(target_table)
        except Exception:
            baseline_rows = 0

        t0 = time.monotonic()
        thread = threading.Thread(
            target=_worker, name=f"mk-gcs-load-{query_id[:8]}", daemon=True
        )
        thread.start()

        # Separate polling client — clickhouse-connect isn't thread-safe.
        poll_client = clickhouse_connect.get_client(**self._conn_params)
        last_sample = ProgressSample(0, 0, 0)
        try:
            while thread.is_alive():
                time.sleep(poll_interval_seconds)
                try:
                    rows = poll_client.query(
                        "SELECT read_bytes, total_rows_approx, elapsed "
                        "FROM clusterAllReplicas('default', system.processes) "
                        f"WHERE query_id = '{query_id}' "
                        "ORDER BY elapsed DESC LIMIT 1"
                    ).result_rows
                except Exception:
                    rows = []
                if rows:
                    rb, tra, elapsed = rows[0]
                    sample = ProgressSample(
                        bytes_done=int(rb or 0),
                        rows_done=int(tra or 0),
                        elapsed_ms=int(float(elapsed or 0) * 1000),
                    )
                    last_sample = sample
                    if on_progress:
                        try:
                            on_progress(sample)
                        except Exception:
                            pass  # progress reporting must never crash the load
        finally:
            try:
                poll_client.close()
            except Exception:
                pass

        thread.join()
        seconds = round(time.monotonic() - t0, 3)
        if outcome["error"] is not None:
            raise outcome["error"]

        final_rows = max(0, self.count_rows(target_table) - baseline_rows)
        return LoadResult(
            rows_done=final_rows,
            bytes_read=last_sample.bytes_done,
            seconds=seconds,
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
