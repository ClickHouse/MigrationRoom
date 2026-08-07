"""Unit tests for DatabricksSource's pure helpers.

These are the parts testable without a Databricks workspace. The class
imports `databricks.sql` lazily inside __init__, so importing the module
needs no connector installed.

Run from the repo root:
    python3 -m pytest tests/test_databricks_source.py -v
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "migration-runner"))

from migrationkit.sources.databricks import (  # noqa: E402
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
