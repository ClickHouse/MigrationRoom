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
