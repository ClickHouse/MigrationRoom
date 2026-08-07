"""Unit tests for the setup-workload SQL statement/directive parser.

Run from the repo root:
    python3 -m pytest tests/test_setup_workload_parser.py -v
"""
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "sources" / "databricks" / "scripts")
)

from setup_workload import parse_statements  # noqa: E402


def test_plain_statements_are_parsed():
    parsed = parse_statements("SELECT 1;\nSELECT 2;\n")
    assert [(s, k) for s, k, _ in parsed] == [("SELECT 1", "plain"), ("SELECT 2", "plain")]


def test_comments_are_dropped():
    parsed = parse_statements("-- a comment\nSELECT 1;\n")
    assert len(parsed) == 1
    assert parsed[0][0] == "SELECT 1"


def test_requires_directive_attaches_hint():
    sql = "-- @requires: VARIANT needs DBSQL 2024.35+\nALTER TABLE t ADD COLUMN v VARIANT;\n"
    statement, kind, hint = parse_statements(sql)[0]
    assert kind == "required"
    assert statement.startswith("ALTER TABLE")
    assert "2024.35" in hint


def test_optional_directive_attaches_hint():
    sql = "-- @optional: needs serverless\nCREATE MATERIALIZED VIEW v AS SELECT 1;\n"
    statement, kind, hint = parse_statements(sql)[0]
    assert kind == "optional"
    assert "serverless" in hint


def test_directive_applies_only_to_the_next_statement():
    sql = (
        "-- @optional: first only\n"
        "SELECT 1;\n"
        "SELECT 2;\n"
    )
    parsed = parse_statements(sql)
    assert parsed[0][1] == "optional"
    assert parsed[1][1] == "plain"


def test_multiline_statement_is_kept_together():
    sql = "CREATE TABLE t (\n  a INT,\n  b INT\n);\n"
    parsed = parse_statements(sql)
    assert len(parsed) == 1
    assert "a INT" in parsed[0][0] and "b INT" in parsed[0][0]


def test_final_statement_without_semicolon_is_kept():
    parsed = parse_statements("SELECT 1")
    assert parsed[0][0] == "SELECT 1"


def test_empty_input_yields_nothing():
    assert parse_statements("\n-- only a comment\n") == []
