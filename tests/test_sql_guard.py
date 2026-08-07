"""Unit tests for the Databricks MCP read-only statement guard.

Run from the repo root:
    python3 -m pip install -r tests/requirements.txt
    python3 -m pytest tests/test_sql_guard.py -v

sql_guard.py classifies statements by tokenizing/parsing them with sqlglot's
Databricks dialect rather than hand-lexing, because three earlier review
rounds each found a real bypass in the hand-lexed version (CTE-prefixed DML,
a backtick identifier that blinded the CTE scanner, and comment/string
lexing that diverged from Spark's grammar). The must-allow/must-reject
lists below are the cases empirically verified against sqlglot 30.15.0
during the design of this rewrite, including all three of those bypasses.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "databricks-mcp"))

from sql_guard import SqlNotAllowed, guard  # noqa: E402

# --- Verified cases -------------------------------------------------------
#
# The two entries containing a literal carriage return are written with the
# "\r" escape below (not a raw CR byte) so they survive editors/git intact;
# Python turns the escape into an actual CR character at parse time.

MUST_ALLOW = [
    "SELECT 1",
    "SELECT 1;",
    "select c from customer",
    "(SELECT 1)",
    "SELECT 1 UNION ALL SELECT 2",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "WITH t (n) AS (VALUES (1)) SELECT * FROM t",
    "WITH RECURSIVE r (n) AS (VALUES (1) UNION ALL SELECT n+1 FROM r WHERE n<6) SELECT * FROM r",
    "SELECT 1 FROM `a;b`",
    "SELECT 'a;b' AS s",
    "SELECT 1 AS `a--b` FROM t",
    "SHOW CATALOGS",
    "DESCRIBE TABLE m.t.o",
    "DESCRIBE DETAIL m.t.l",
    "DESCRIBE HISTORY m.t.l LIMIT 5",
    "DESC DETAIL m.t.l",
    "EXPLAIN SELECT 1",
]

MUST_REJECT = [
    # The first five are the previously-verified bypasses.
    "WITH x AS (SELECT 1) INSERT INTO orders SELECT * FROM x",
    "WITH x AS (SELECT 1 AS `a) SELECT 2 AS b` FROM t) INSERT INTO orders SELECT * FROM x",
    "SHOW TABLES -- x\r; DROP TABLE t",
    "SELECT 1 -- c\r; INSERT INTO orders VALUES (1)",
    "SELECT 'a\\' AS x, ' ; DROP TABLE t",
    "SELECT 1; DROP TABLE t",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a=1",
    "DELETE FROM t",
    "DROP TABLE t",
    "CREATE TABLE t (a INT)",
    "ALTER TABLE t ADD COLUMN b INT",
    "TRUNCATE TABLE t",
    "COPY INTO t FROM 's3://b/k'",
    "GRANT SELECT ON TABLE t TO `u`",
    "MERGE INTO t USING s ON t.a=s.a WHEN MATCHED THEN UPDATE SET t.b=s.b",
    "WITH x AS (SELECT 1) MERGE INTO t USING x ON t.a=x.a WHEN MATCHED THEN DELETE",
    "OPTIMIZE m.t.l ZORDER BY (a)",
    "VACUUM m.t.l",
    "   ",  # empty / whitespace-only
]


@pytest.mark.parametrize(
    "sql", MUST_ALLOW, ids=[f"allow-{i:02d}" for i in range(len(MUST_ALLOW))]
)
def test_verified_read_only_statements_are_allowed(sql):
    assert guard(sql)


@pytest.mark.parametrize(
    "sql", MUST_REJECT, ids=[f"reject-{i:02d}" for i in range(len(MUST_REJECT))]
)
def test_verified_mutations_and_malformed_input_are_rejected(sql):
    with pytest.raises(SqlNotAllowed):
        guard(sql)


def test_multi_statement_error_message_mentions_one_statement():
    with pytest.raises(SqlNotAllowed) as excinfo:
        guard("SELECT 1; DROP TABLE t")
    assert "one statement" in str(excinfo.value)


def test_mutation_error_message_points_to_clickhousectl():
    with pytest.raises(SqlNotAllowed) as excinfo:
        guard("DROP TABLE t")
    assert "clickhousectl" in str(excinfo.value)


def test_trailing_comment_after_semicolon_is_not_a_second_statement():
    # Regression: sqlglot.parse() emits a synthetic exp.Semicolon node to
    # carry a comment that trails the terminator. That node is truthy, so
    # it survived the `if s` None-filter and made this look like two
    # statements. It must not, since it fails closed either way but a
    # trailing comment on a query is ordinary input.
    assert guard("SELECT 1; -- comment") == "SELECT 1\nLIMIT 200"


def test_trailing_comment_fix_does_not_widen_the_multi_statement_gate():
    # Paired negative case: a *real* second statement after the comment
    # must still be rejected. The exp.Semicolon filter must not swallow
    # an actual statement, only the content-free placeholder node.
    with pytest.raises(SqlNotAllowed):
        guard("SELECT 1; -- comment\nDROP TABLE t")


# --- LIMIT injection -------------------------------------------------------


def test_limit_word_inside_backtick_alias_gets_exactly_one_real_limit():
    out = guard("SELECT 1 AS `x LIMIT 5` FROM t")
    # The backtick alias contains the word LIMIT verbatim; only the
    # appended clause should introduce a *new* line starting with LIMIT.
    assert out.count("\nLIMIT") == 1
    assert out.endswith("LIMIT 200")


@pytest.mark.parametrize(
    "sql",
    [
        "SHOW CATALOGS",
        "DESCRIBE TABLE m.t.o",
        "DESC DETAIL m.t.l",
        "EXPLAIN SELECT 1",
    ],
)
def test_show_describe_explain_receive_no_limit(sql):
    assert "LIMIT" not in guard(sql)


def test_limit_is_injected_using_max_rows():
    assert guard("SELECT * FROM orders", max_rows=50).endswith("LIMIT 50")


def test_existing_limit_is_not_doubled():
    out = guard("SELECT * FROM orders LIMIT 5", max_rows=50)
    assert out.lower().count("limit") == 1


def test_union_without_limit_gets_one_appended():
    out = guard("SELECT 1 UNION ALL SELECT 2", max_rows=10)
    assert out.count("\nLIMIT") == 1
    assert out.endswith("LIMIT 10")


def test_union_with_existing_limit_is_not_doubled():
    out = guard("SELECT 1 UNION ALL SELECT 2 LIMIT 1", max_rows=10)
    assert out.lower().count("limit") == 1


def test_parenthesized_select_without_limit_gets_one_appended():
    # Regression: a top-level `(SELECT ...)` parses to exp.Subquery, not
    # exp.Select/exp.Union, and was previously exempted from LIMIT
    # injection entirely — silently defeating the row cap.
    out = guard("(SELECT * FROM orders)", max_rows=50)
    assert out == "(SELECT * FROM orders)\nLIMIT 50"


def test_parenthesized_select_with_existing_limit_is_not_doubled():
    # The existing LIMIT lives on the inner Select, not the outer
    # Subquery; both slots must be checked or this gets a second LIMIT.
    out = guard("(SELECT * FROM orders LIMIT 5)", max_rows=50)
    assert out == "(SELECT * FROM orders LIMIT 5)"
    assert out.lower().count("limit") == 1


def test_parenthesized_union_without_limit_gets_one_appended():
    out = guard("(SELECT * FROM a UNION ALL SELECT * FROM b)", max_rows=50)
    assert out == "(SELECT * FROM a UNION ALL SELECT * FROM b)\nLIMIT 50"


def test_parenthesized_union_with_existing_limit_is_not_doubled():
    out = guard(
        "(SELECT * FROM a UNION ALL SELECT * FROM b LIMIT 5)", max_rows=50
    )
    assert out == "(SELECT * FROM a UNION ALL SELECT * FROM b LIMIT 5)"
    assert out.lower().count("limit") == 1


# --- guard returns the original text, never a sqlglot regeneration --------


def test_guard_returns_original_text_with_only_limit_appended():
    sql = "SELECT  1,   `weird  Name` FROM   t"
    out = guard(sql, max_rows=77)
    assert out == sql + "\nLIMIT 77"


def test_guard_preserves_original_formatting_when_no_limit_is_appended():
    sql = "SHOW   CATALOGS"
    assert guard(sql) == sql


def test_guard_strips_the_trailing_semicolon_before_appending_limit():
    out = guard("SELECT 1;", max_rows=5)
    assert out == "SELECT 1\nLIMIT 5"


def test_guard_strips_trailing_semicolon_when_no_limit_is_needed():
    out = guard("SHOW CATALOGS;")
    assert out == "SHOW CATALOGS"


def test_guard_does_not_double_strip_semicolon_inside_backtick():
    sql = "SELECT 1 FROM `a;b`"
    out = guard(sql, max_rows=5)
    assert out == sql + "\nLIMIT 5"


# --- misc -------------------------------------------------------------


def test_empty_statement_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("")


def test_whitespace_only_statement_is_rejected():
    with pytest.raises(SqlNotAllowed):
        guard("   \n\t  ")
