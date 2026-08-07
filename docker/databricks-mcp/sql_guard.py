"""Read-only statement guard for the Databricks source MCP.

Deliberately free of `mcp` and `databricks` imports so it can be unit-tested
on the host without the server's dependencies installed.

A Databricks SQL warehouse has no session-level read-only switch, so the
only place we can refuse a mutation is here. Grants are the second layer
(see sources/databricks/GUIDE.md) — this is the first.

This module used to classify statements by hand-lexing the leading keyword
and scanning for semicolons/comments/quotes with regexes. Three review
rounds each found a real bypass in that approach (a CTE prefixing DML, a
backtick-quoted identifier blinding the CTE scanner, and comment/string
lexing that diverged from Spark's grammar). Hand-matching Spark's lexical
grammar is not a small task, so this rewrite delegates it to sqlglot's
Databricks dialect tokenizer/parser instead of re-implementing it. A
CTE-prefixed INSERT then simply *is* an `exp.Insert` node, and correct
comment/string/identifier handling comes from a real tokenizer.
"""
from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp
from sqlglot.dialects import Databricks
from sqlglot.errors import ParseError, TokenError

# sqlglot logs a WARNING ("... contains unsupported syntax. Falling back to
# parsing as a 'Command'.") for every SHOW/EXPLAIN/OPTIMIZE/VACUUM statement,
# because those are intentionally handled via the exp.Command fallback below.
# That is expected here, not a problem to surface on every legitimate SHOW.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# Node types that represent a read-only statement once successfully parsed.
READ_ONLY_NODES = (exp.Select, exp.Union, exp.Subquery, exp.Describe)

# Leading verbs that are read-only even when sqlglot cannot build a full AST
# for the statement (Databricks-specific extensions like `DESC DETAIL`) or
# falls back to a generic exp.Command node (e.g. `SHOW CATALOGS`). Kept
# narrow on purpose: this is NOT "allow anything unparseable", it is "allow
# only these four verbs when parsing can't tell us more."
READ_ONLY_VERBS = frozenset({"SHOW", "EXPLAIN", "DESCRIBE", "DESC"})

_DATABRICKS = Databricks()
_DIALECT = "databricks"


class SqlNotAllowed(ValueError):
    """Raised when a statement is not a single read-only statement."""


def _strip_trailing_semicolon(sql: str, toks) -> str:
    """Return `sql` with its single legal trailing ';' (if any) removed.

    A trailing semicolon is a statement terminator, not statement text; if
    we appended a LIMIT clause after it we'd produce invalid SQL. Everything
    else about the original text — including internal whitespace and
    formatting — is preserved untouched.
    """
    if toks and toks[-1].token_type == sqlglot.TokenType.SEMICOLON:
        return sql[: toks[-1].start].rstrip()
    return sql.strip()


def _unwrap_subquery(node: exp.Expression) -> exp.Expression:
    """Follow `Subquery.this` down to the innermost wrapped query.

    `(SELECT ...)` parses to `exp.Subquery` wrapping an `exp.Select` (or
    `exp.Union`), and a `LIMIT` already present in the source SQL may live
    on that inner node rather than on the `Subquery` itself — e.g.
    `(SELECT * FROM t LIMIT 5)` puts the limit on the inner `Select`, while
    `(SELECT * FROM t) LIMIT 5` puts it on the outer `Subquery`. Checking
    only one of the two slots would miss an existing limit and double it.
    """
    while isinstance(node, exp.Subquery) and node.this is not None:
        node = node.this
    return node


def _existing_limit(root: exp.Expression) -> exp.Expression | None:
    """Return the existing LIMIT clause for `root`, if any, checking both
    the node itself and, for a parenthesized query, the query it wraps."""
    limit = root.args.get("limit")
    if limit is not None:
        return limit
    inner = _unwrap_subquery(root)
    if inner is not root:
        return inner.args.get("limit")
    return None


def guard(sql: str, max_rows: int = 200) -> str:
    """Validate `sql` as one read-only statement and return it ready to run.

    A row cap is appended to SELECT/UNION statements — including ones
    wrapped in parentheses, e.g. `(SELECT ...)` — that don't already have
    one, so an unbounded scan can't stream a whole fact table into the
    chat context. The original statement text is returned (never a
    sqlglot-regenerated form) with only the trailing statement terminator
    removed and, when applicable, a LIMIT clause appended.

    Raises SqlNotAllowed for anything else.
    """
    if not sql or not sql.strip():
        raise SqlNotAllowed("empty statement; nothing to run")

    try:
        toks = _DATABRICKS.tokenize(sql)
    except TokenError as exc:
        raise SqlNotAllowed(
            f"could not tokenize this statement, so it cannot be verified "
            f"as read-only ({exc})"
        ) from exc

    # A SEMICOLON anywhere but the final token means multiple statements
    # were sent. A single trailing ';' is legal and handled above/below.
    if any(t.token_type == sqlglot.TokenType.SEMICOLON for t in toks[:-1]):
        raise SqlNotAllowed(
            "multiple statements are not allowed; send one statement at a time"
        )

    try:
        # `exp.Semicolon` is a content-free node sqlglot appends to hold a
        # comment that trails the statement terminator (e.g. `SELECT 1; --
        # comment`); it is truthy, so it must be filtered alongside `None`
        # or a harmless trailing comment reads as a second statement.
        stmts = [
            s
            for s in sqlglot.parse(sql, dialect=_DIALECT)
            if s and not isinstance(s, exp.Semicolon)
        ]
    except (ParseError, TokenError) as exc:
        # Unparseable is rejected UNLESS the statement's leading token is a
        # read-only verb — this covers Databricks extensions sqlglot's
        # parser does not model, e.g. `DESC DETAIL`. We cannot safely modify
        # text we could not parse, so it is returned as-is (minus a trailing
        # terminator).
        if toks and str(toks[0].text).upper() in READ_ONLY_VERBS:
            return _strip_trailing_semicolon(sql, toks)
        raise SqlNotAllowed(
            f"this MCP is read-only and could not parse this statement to "
            f"confirm that ({exc}). If this is DDL/DML for the target, use "
            "the clickhousectl MCP instead."
        ) from exc

    if len(stmts) != 1:
        raise SqlNotAllowed(
            "expected exactly one statement; send one statement at a time"
        )

    root = stmts[0]

    # exp.Command is sqlglot's "I did not model this statement" fallback.
    # SHOW/EXPLAIN land here — so do OPTIMIZE and VACUUM, which is exactly
    # why the verb check matters: allowing all Command nodes would be a
    # new bypass.
    if isinstance(root, exp.Command):
        verb = str(root.this).upper()
        if verb not in READ_ONLY_VERBS:
            raise SqlNotAllowed(
                f"this MCP is read-only; '{verb}' is not permitted. Run "
                "DDL/DML against the target via the clickhousectl MCP "
                "instead."
            )
        return _strip_trailing_semicolon(sql, toks)

    if not isinstance(root, READ_ONLY_NODES):
        raise SqlNotAllowed(
            f"this MCP is read-only; '{type(root).__name__}' statements "
            "are not permitted. Run DDL/DML against the target via the "
            "clickhousectl MCP instead."
        )

    body = _strip_trailing_semicolon(sql, toks)
    if (
        isinstance(root, (exp.Select, exp.Union, exp.Subquery))
        and _existing_limit(root) is None
    ):
        body = f"{body}\nLIMIT {max_rows}"
    return body
