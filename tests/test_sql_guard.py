"""Unit tests for the Databricks MCP read-only statement guard.

Run from the repo root:
    python3 -m pip install -r tests/requirements.txt
    python3 -m pytest tests/test_sql_guard.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "databricks-mcp"))

from sql_guard import READ_ONLY_LEADING_KEYWORDS, SqlNotAllowed, guard  # noqa: E402


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select c_custkey from customer",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SHOW CATALOGS",
        "DESCRIBE TABLE migration_demo.tpch.orders",
        "DESC DETAIL migration_demo.tpch.lineitem",
        "EXPLAIN SELECT 1",
        "  \n  SELECT 1  \n ",
        "(SELECT 1)",
    ],
)
def test_read_only_statements_are_allowed(sql):
    assert guard(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE t (a INT)",
        "ALTER TABLE t ADD COLUMN b INT",
        "MERGE INTO t USING s ON t.a = s.a",
        "TRUNCATE TABLE t",
        "COPY INTO t FROM 's3://b/k'",
        "GRANT SELECT ON TABLE t TO `u`",
    ],
)
def test_mutating_statements_are_rejected(sql):
    with pytest.raises(SqlNotAllowed):
        guard(sql)


def test_multi_statement_is_rejected():
    with pytest.raises(SqlNotAllowed) as excinfo:
        guard("SELECT 1; DROP TABLE t")
    assert "one statement" in str(excinfo.value)


def test_trailing_semicolon_is_not_multi_statement():
    assert guard("SELECT 1;").startswith("SELECT 1")


def test_semicolon_inside_string_literal_is_not_multi_statement():
    assert guard("SELECT 'a;b' AS s")


def test_comment_hidden_mutation_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("-- harmless\nDROP TABLE t")


def test_block_comment_hidden_mutation_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("/* SELECT */ DROP TABLE t")


def test_limit_is_injected_when_absent():
    assert guard("SELECT * FROM orders", max_rows=50).endswith("LIMIT 50")


def test_existing_limit_is_not_doubled():
    out = guard("SELECT * FROM orders LIMIT 5", max_rows=50)
    assert out.lower().count("limit") == 1


def test_show_does_not_get_a_limit():
    assert "LIMIT" not in guard("SHOW CATALOGS")


def test_empty_statement_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("   ")


def test_keyword_set_is_read_only():
    assert READ_ONLY_LEADING_KEYWORDS == frozenset(
        {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
    )
