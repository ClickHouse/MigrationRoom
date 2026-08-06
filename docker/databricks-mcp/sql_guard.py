"""Read-only statement guard for the Databricks source MCP.

Deliberately free of `mcp` and `databricks` imports so it can be unit-tested
on the host without the server's dependencies installed.

A Databricks SQL warehouse has no session-level read-only switch, so the
only place we can refuse a mutation is here. Grants are the second layer
(see sources/databricks/GUIDE.md) — this is the first.
"""
from __future__ import annotations

import re

READ_ONLY_LEADING_KEYWORDS = frozenset(
    {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
)

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_SINGLE_QUOTED = re.compile(r"'(?:''|[^'])*'")
_DOUBLE_QUOTED = re.compile(r'"(?:""|[^"])*"')
_LEADING_WORD = re.compile(r"[(\s]*([A-Za-z_]+)")
_TRAILING_LIMIT = re.compile(r"\blimit\b\s+\d+\s*$", re.I)


class SqlNotAllowed(ValueError):
    """Raised when a statement is not a single read-only statement."""


def strip_comments(sql: str) -> str:
    """Remove block and line comments so they can't hide a leading keyword."""
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))


def leading_keyword(sql: str) -> str:
    """First bare word of the statement, upper-cased. Leading parens and
    comments are skipped so `(SELECT 1)` and `/* c */ SELECT 1` both read
    as SELECT."""
    match = _LEADING_WORD.match(strip_comments(sql).strip())
    return match.group(1).upper() if match else ""


def is_multi_statement(sql: str) -> bool:
    """True when a ';' separates statements. Semicolons inside string
    literals and comments don't count, and a single trailing ';' is fine."""
    body = strip_comments(sql)
    body = _DOUBLE_QUOTED.sub('""', _SINGLE_QUOTED.sub("''", body))
    return ";" in body.strip().rstrip(";")


def guard(sql: str, max_rows: int = 200) -> str:
    """Validate `sql` as one read-only statement and return it ready to run.

    A row cap is appended to SELECT/WITH statements that don't already end
    in one, so an unbounded scan can't stream a whole fact table into the
    chat context.

    Raises SqlNotAllowed for anything else."""
    if not sql or not sql.strip():
        raise SqlNotAllowed("empty statement")
    if is_multi_statement(sql):
        raise SqlNotAllowed(
            "multiple statements are not allowed; send one statement"
        )
    keyword = leading_keyword(sql)
    if keyword not in READ_ONLY_LEADING_KEYWORDS:
        allowed = ", ".join(sorted(READ_ONLY_LEADING_KEYWORDS))
        raise SqlNotAllowed(
            f"this MCP is read-only; only {allowed} are permitted, "
            f"got {keyword or '?'}. Run DDL against the target via "
            f"the clickhousectl MCP instead."
        )
    body = sql.strip().rstrip(";").rstrip()
    if keyword in {"SELECT", "WITH"} and not _TRAILING_LIMIT.search(
        strip_comments(body)
    ):
        body = f"{body}\nLIMIT {max_rows}"
    return body
