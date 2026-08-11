"""Unit tests for ClickHouseTarget's introspection contract.

The case-map produced here decides which row keys reach an INSERT, so what it
excludes is as load-bearing as what it includes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "migration-runner"))

from migrationkit.targets.clickhouse import ClickHouseTarget  # noqa: E402


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeClient:
    """Records the query it was handed and replays canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.query_text = None
        self.parameters = None

    def query(self, text, parameters=None):
        self.query_text = text
        self.parameters = parameters
        return _FakeResult(self._rows)


def _target_with(client):
    t = ClickHouseTarget.__new__(ClickHouseTarget)  # bypass __init__/connect
    t._client = client
    return t


def test_introspect_columns_excludes_alias_and_materialized():
    """ClickHouse computes ALIAS/MATERIALIZED columns and rejects an INSERT
    that supplies them, so they must not appear in the case-map. Otherwise a
    source-side generated column (Databricks `o_orderyear`) matches the
    target's ALIAS column and every batch fails."""
    client = _FakeClient([("o_orderkey",), ("o_comment",)])
    case_map = _target_with(client).introspect_columns("migration_demo", "orders")

    assert case_map == {"o_orderkey": "o_orderkey", "o_comment": "o_comment"}
    sql = " ".join(client.query_text.split())
    assert "default_kind NOT IN ('ALIAS', 'MATERIALIZED')" in sql, (
        "the exclusion must happen in SQL — every column system.columns "
        "returns lands in the case-map"
    )


def test_introspect_columns_still_filters_by_database_and_table():
    client = _FakeClient([("a",)])
    _target_with(client).introspect_columns("db1", "t1")
    assert client.parameters == {"db": "db1", "tbl": "t1"}


def test_introspect_columns_returns_none_when_table_is_absent():
    """Preflight turns None into a clear "run step 1 again" error, so this
    must stay distinct from an empty map."""
    assert _target_with(_FakeClient([])).introspect_columns("db", "nope") is None


def test_introspect_columns_returns_none_when_introspection_raises():
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("no permission on system.columns")

    assert _target_with(_Boom()).introspect_columns("db", "t") is None


def test_introspect_columns_lowercases_keys_and_keeps_actual_names():
    client = _FakeClient([("O_OrderKey",)])
    assert _target_with(client).introspect_columns("db", "t") == {"o_orderkey": "O_OrderKey"}
