"""Unit tests for DatabricksSource's pure helpers.

These are the parts testable without a Databricks workspace. The class
imports `databricks.sql` lazily inside __init__, so importing the module
needs no connector installed.

Run from the repo root:
    python3 -m pytest tests/test_databricks_source.py -v
"""
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "migration-runner"))

from migrationkit.sources.databricks import (  # noqa: E402
    DatabricksSource,
    normalize_host,
    parquet_only,
    split_namespace,
)


@dataclass
class FakeObject:
    key: str
    size: int


def test_split_namespace_returns_catalog_and_schema():
    assert split_namespace("migration_demo.tpch") == ("migration_demo", "tpch")


def test_split_namespace_tolerates_whitespace():
    assert split_namespace("  migration_demo . tpch ") == ("migration_demo", "tpch")


@pytest.mark.parametrize("bad", ["", "   ", "tpch", "a.b.c", ".", "a."])
def test_split_namespace_rejects_anything_but_two_parts(bad):
    with pytest.raises(ValueError) as excinfo:
        split_namespace(bad)
    assert "DATABRICKS_NAMESPACE" in str(excinfo.value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://dbc-abc.cloud.databricks.com", "dbc-abc.cloud.databricks.com"),
        ("http://dbc-abc.cloud.databricks.com/", "dbc-abc.cloud.databricks.com"),
        ("dbc-abc.cloud.databricks.com", "dbc-abc.cloud.databricks.com"),
        ("  https://dbc-abc.cloud.databricks.com/  ", "dbc-abc.cloud.databricks.com"),
    ],
)
def test_normalize_host_strips_scheme_and_trailing_slash(raw, expected):
    assert normalize_host(raw) == expected


def test_parquet_only_drops_commit_protocol_markers():
    objects = [
        FakeObject("p/run/lineitem/part-00000-abc.snappy.parquet", 1000),
        FakeObject("p/run/lineitem/part-00001-def.snappy.parquet", 2000),
        FakeObject("p/run/lineitem/_SUCCESS", 0),
        FakeObject("p/run/lineitem/_committed_12345", 120),
        FakeObject("p/run/lineitem/_started_12345", 80),
    ]
    kept = parquet_only(objects)
    assert len(kept) == 2
    assert sum(o.size for o in kept) == 3000


def test_parquet_only_is_case_insensitive():
    assert len(parquet_only([FakeObject("a/B.PARQUET", 1)])) == 1


def _unconnected_source(catalog, schema):
    """Build a DatabricksSource without touching __init__ (no network,
    no connector import) — enough to exercise the pure `_fq` logic."""
    src = DatabricksSource.__new__(DatabricksSource)
    src.catalog = catalog
    src.schema = schema
    return src


def test_fq_qualifies_a_bare_table_name():
    src = _unconnected_source("migration_demo", "tpch")
    assert src._fq("orders") == "migration_demo.tpch.orders"


def test_fq_passes_through_an_already_qualified_name_unchanged():
    src = _unconnected_source("migration_demo", "tpch")
    assert src._fq("other_catalog.other_schema.orders") == "other_catalog.other_schema.orders"


def test_fq_raises_for_bare_name_without_a_configured_namespace():
    src = _unconnected_source(None, None)
    with pytest.raises(ValueError):
        src._fq("orders")


class _FakeHTTPResponse:
    """Minimal stand-in for what `urllib.request.urlopen` returns — supports
    the context-manager protocol and the `.read()` that `json.load` uses,
    without touching the network."""

    def __init__(self, body):
        self._raw = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._raw


@pytest.mark.parametrize(
    "malformed_body",
    [
        None,  # null body
        [],  # bare list body
        {"res": "not-a-list"},  # res is a string
        {"res": ["not-a-dict"]},  # res item not a dict
        {"res": [{"metrics": "not-a-dict"}]},  # metrics not a dict
    ],
)
def test_server_ms_from_history_api_returns_none_on_malformed_response(
    monkeypatch, malformed_body
):
    """Regression test: the response shape is an admitted guess against a
    live API. If the guess is wrong, this must degrade to None, never
    raise — an uncaught exception here fails the whole benchmark row
    instead of falling back to wall_ms."""
    src = DatabricksSource.__new__(DatabricksSource)
    src._host = "dbc-abc.cloud.databricks.com"
    src._token = "fake-token"

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **kw: _FakeHTTPResponse(malformed_body)
    )

    assert src._server_ms_from_history_api("stmt-id-123") is None


# ── iter_batches value normalisation ─────────────────────────────────────
#
# The connector returns ARRAY<...> columns as numpy.ndarray while every other
# type arrives as a plain Python object. That difference is invisible until
# something truth-tests the value, and `if events:` is the most natural thing
# to write — it raises "ValueError: The truth value of an array with more than
# one element is ambiguous". A real migration died on lineitem's
# ARRAY<STRUCT<...>> column at row 0 for exactly this reason, so iter_batches
# hands downstream code ordinary Python values.


class _FakeCursor:
    """Minimal DB-API cursor over canned rows, with Databricks' `description`
    shape: (name, declared_type, ...)."""

    def __init__(self, description, rows):
        self.description = description
        self._rows = list(rows)
        self.closed = False

    def execute(self, query, *args):
        self.query = query

    def fetchmany(self, n):
        chunk, self._rows = self._rows[:n], self._rows[n:]
        return chunk

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _source_with(cursor):
    src = DatabricksSource.__new__(DatabricksSource)  # bypass __init__/connect
    src._conn = _FakeConn(cursor)
    return src


def test_iter_batches_converts_numpy_arrays_to_lists():
    numpy = pytest.importorskip("numpy")
    events = numpy.array([{"status": "PACKED"}, {"status": "SHIPPED"}], dtype=object)
    cursor = _FakeCursor(
        [("L_ORDERKEY", "bigint"), ("L_SHIPPING_EVENTS", "array")],
        [(1, events)],
    )
    batches = list(_source_with(cursor).iter_batches("SELECT *", 10))

    value = batches[0][0]["l_shipping_events"]
    assert isinstance(value, list), f"expected list, got {type(value).__name__}"
    assert value == [{"status": "PACKED"}, {"status": "SHIPPED"}]
    # The whole point: this is what the agent writes and it must not raise.
    assert bool(value) is True


def test_iter_batches_leaves_null_arrays_as_none():
    cursor = _FakeCursor(
        [("L_ORDERKEY", "bigint"), ("L_SHIPPING_EVENTS", "array")],
        [(1, None)],
    )
    batches = list(_source_with(cursor).iter_batches("SELECT *", 10))
    assert batches[0][0]["l_shipping_events"] is None


def test_iter_batches_normalises_arrays_nested_inside_arrays():
    numpy = pytest.importorskip("numpy")
    inner = numpy.array([1, 2], dtype=object)
    outer = numpy.array([inner], dtype=object)
    cursor = _FakeCursor([("L_NESTED", "array")], [(outer,)])
    batches = list(_source_with(cursor).iter_batches("SELECT *", 10))
    assert batches[0][0]["l_nested"] == [[1, 2]]


def test_iter_batches_does_not_touch_non_array_columns():
    """Only ARRAY columns are rewritten — everything else passes through
    untouched, so the conversion costs nothing on ordinary columns."""
    sentinel = object()
    cursor = _FakeCursor(
        [("L_ORDERKEY", "bigint"), ("L_COMMENT", "string")],
        [(1, sentinel)],
    )
    batches = list(_source_with(cursor).iter_batches("SELECT *", 10))
    assert batches[0][0]["l_comment"] is sentinel


def test_iter_batches_converts_maps_to_dicts():
    """Databricks returns MAP as a list of pairs; ClickHouse's Map wants a
    mapping, and clickhouse-connect raises "'list' object has no attribute
    'keys'" on the list form."""
    cursor = _FakeCursor(
        [("L_ORDERKEY", "bigint"), ("L_ATTRIBUTES", "map")],
        [(1, [("carrier", "UPS"), ("fragile", "false")])],
    )
    batches = list(_source_with(cursor).iter_batches("SELECT *", 10))
    assert batches[0][0]["l_attributes"] == {"carrier": "UPS", "fragile": "false"}


def test_iter_batches_leaves_null_maps_as_none():
    cursor = _FakeCursor([("L_ATTRIBUTES", "map")], [(None,)])
    batches = list(_source_with(cursor).iter_batches("SELECT *", 10))
    assert batches[0][0]["l_attributes"] is None


def test_iter_batches_leaves_an_empty_map_as_an_empty_dict():
    cursor = _FakeCursor([("L_ATTRIBUTES", "map")], [([],)])
    batches = list(_source_with(cursor).iter_batches("SELECT *", 10))
    assert batches[0][0]["l_attributes"] == {}


def test_map_conversion_does_not_reinterpret_non_pair_shapes():
    """A declared map whose value is not a list of pairs is passed through
    rather than guessed at."""
    weird = [("a", 1, 2), ("b", 3, 4)]
    cursor = _FakeCursor([("L_ATTRIBUTES", "map")], [(weird,)])
    batches = list(_source_with(cursor).iter_batches("SELECT *", 10))
    assert batches[0][0]["l_attributes"] == weird


def test_array_of_two_field_structs_is_not_turned_into_a_map():
    """An ARRAY column is normalised by the array path, never the map path —
    so an array of 2-tuples stays a list."""
    cursor = _FakeCursor([("L_PAIRS", "array")], [([("a", 1), ("b", 2)],)])
    batches = list(_source_with(cursor).iter_batches("SELECT *", 10))
    assert batches[0][0]["l_pairs"] == [("a", 1), ("b", 2)]
