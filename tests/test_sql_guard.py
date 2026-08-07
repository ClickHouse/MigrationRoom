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


# CTE-prefixed DML rejection tests
@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) INSERT INTO t VALUES (1)",
        "WITH x AS (SELECT 1) UPDATE t SET a = 1",
        "WITH x AS (SELECT 1) DELETE FROM t",
        "WITH x AS (SELECT 1) MERGE INTO t USING s ON t.a = s.a",
    ],
)
def test_cte_with_dml_is_rejected(sql):
    with pytest.raises(SqlNotAllowed) as excinfo:
        guard(sql)
    assert "CTE-prefixed DML" in str(excinfo.value)


def test_cte_with_recursive_and_insert_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("WITH RECURSIVE x AS (SELECT 1) INSERT INTO t VALUES (1)")


def test_cte_with_column_list_and_insert_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("WITH t (a, b) AS (SELECT 1, 2) INSERT INTO t VALUES (1, 2)")


def test_cte_with_multiple_ctes_and_insert_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("WITH a AS (SELECT 1), b AS (SELECT 2) INSERT INTO t SELECT * FROM a")


def test_cte_with_nested_parens_and_insert_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard(
            "WITH x AS (SELECT 1 FROM (SELECT 2) y) INSERT INTO t SELECT * FROM x"
        )


def test_cte_with_string_literal_paren_and_insert_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("WITH x AS (SELECT '(' AS s) INSERT INTO t SELECT * FROM x")


def test_cte_with_comment_hiding_insert_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("WITH x AS (SELECT 1) /* c */ INSERT INTO t VALUES (1)")


def test_cte_with_values_and_insert_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("WITH t (n) AS (VALUES (1)) INSERT INTO t SELECT * FROM t")


def test_cte_with_nested_cte_and_insert_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard(
            "WITH x AS (WITH y AS (SELECT 1) SELECT * FROM y) "
            "INSERT INTO t SELECT * FROM x"
        )


def test_cte_with_select_is_allowed():
    assert guard("WITH x AS (SELECT 1) SELECT * FROM x")


def test_cte_with_multiple_ctes_select_is_allowed():
    assert guard("WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a, b")


def test_cte_with_recursive_select_is_allowed():
    assert guard("WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x")


def test_cte_with_column_list_select_is_allowed():
    assert guard("WITH t (a, b) AS (SELECT 1, 2) SELECT * FROM t")


def test_cte_with_nested_parens_select_is_allowed():
    assert guard(
        "WITH x AS (SELECT 1 FROM (SELECT 2) y) SELECT * FROM x"
    )


def test_cte_with_string_literal_paren_select_is_allowed():
    assert guard("WITH x AS (SELECT '(' AS s) SELECT * FROM x")


def test_cte_with_values_select_is_allowed():
    assert guard("WITH t (n) AS (VALUES (1)) SELECT * FROM t")


def test_cte_with_nested_cte_select_is_allowed():
    assert guard("WITH x AS (WITH y AS (SELECT 1) SELECT * FROM y) SELECT * FROM x")


def test_cte_with_select_gets_limit():
    out = guard("WITH x AS (SELECT 1) SELECT * FROM x", max_rows=50)
    assert out.endswith("LIMIT 50")


def test_cte_with_select_existing_limit_not_doubled():
    out = guard("WITH x AS (SELECT 1) SELECT * FROM x LIMIT 5", max_rows=50)
    assert out.lower().count("limit") == 1


def test_cte_with_multiple_ctes_select_gets_limit():
    out = guard(
        "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a, b", max_rows=50
    )
    assert out.endswith("LIMIT 50")
