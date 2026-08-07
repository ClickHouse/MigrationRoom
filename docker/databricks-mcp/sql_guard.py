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

_LEADING_WORD = re.compile(r"[(\s]*([A-Za-z_]+)")
_TRAILING_LIMIT = re.compile(r"\blimit\b\s+\d+\s*$", re.I)


class SqlNotAllowed(ValueError):
    """Raised when a statement is not a single read-only statement."""


def _neutralize(sql: str) -> str:
    """Neutralize all quoted/commented regions in one pass, preserving length.

    Returns a string of identical length where all characters inside any of
    these five mutually-exclusive regions are replaced by spaces:
    - line comments: -- to end of line (but not the newline itself)
    - block comments: /* ... */
    - single-quoted strings: '...' ('' escapes)
    - double-quoted strings: "..." ("" escapes)
    - backtick identifiers: `...` (`` escapes)

    Whichever region opens first (in reading order) wins; once inside one,
    the others cannot begin. This single-pass approach handles all interactions
    correctly: '/*' is a string, not a comment; `--` is an identifier, not a
    comment; -- 'unclosed remains a comment and eats the rest of the line.
    """
    result = []
    i = 0

    while i < len(sql):
        # Line comment: -- to end of line
        # Must come before backtick/quote checks
        if i + 1 < len(sql) and sql[i : i + 2] == "--":
            result.append(" ")
            result.append(" ")
            i += 2
            # Consume until newline but preserve the newline
            while i < len(sql) and sql[i] not in "\n":
                result.append(" ")
                i += 1
            if i < len(sql) and sql[i] == "\n":
                result.append("\n")
                i += 1
            continue

        # Block comment: /* ... */
        if i + 1 < len(sql) and sql[i : i + 2] == "/*":
            result.append(" ")
            result.append(" ")
            i += 2
            # Consume until */
            while i + 1 < len(sql):
                if sql[i : i + 2] == "*/":
                    result.append(" ")
                    result.append(" ")
                    i += 2
                    break
                result.append(" ")
                i += 1
            continue

        # Single-quoted string: '...' with '' as escape
        if sql[i] == "'":
            result.append(" ")
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    if i + 1 < len(sql) and sql[i + 1] == "'":
                        # Escaped quote
                        result.append(" ")
                        result.append(" ")
                        i += 2
                    else:
                        # End of string
                        result.append(" ")
                        i += 1
                        break
                else:
                    result.append(" ")
                    i += 1
            continue

        # Double-quoted string: "..." with "" as escape
        if sql[i] == '"':
            result.append(" ")
            i += 1
            while i < len(sql):
                if sql[i] == '"':
                    if i + 1 < len(sql) and sql[i + 1] == '"':
                        # Escaped quote
                        result.append(" ")
                        result.append(" ")
                        i += 2
                    else:
                        # End of string
                        result.append(" ")
                        i += 1
                        break
                else:
                    result.append(" ")
                    i += 1
            continue

        # Backtick-quoted identifier: `...` with `` as escape
        if sql[i] == "`":
            result.append(" ")
            i += 1
            while i < len(sql):
                if sql[i] == "`":
                    if i + 1 < len(sql) and sql[i + 1] == "`":
                        # Escaped backtick
                        result.append(" ")
                        result.append(" ")
                        i += 2
                    else:
                        # End of identifier
                        result.append(" ")
                        i += 1
                        break
                else:
                    result.append(" ")
                    i += 1
            continue

        # Regular character
        result.append(sql[i])
        i += 1

    return "".join(result)


def strip_comments(sql: str) -> str:
    """Remove block and line comments so they can't hide a leading keyword."""
    neutralized = _neutralize(sql)
    # Replace spaces that came from neutralization with empty space
    # (but keep real spaces from the original)
    return neutralized


def leading_keyword(sql: str) -> str:
    """First bare word of the statement, upper-cased. Leading parens and
    comments are skipped so `(SELECT 1)` and `/* c */ SELECT 1` both read
    as SELECT."""
    neutralized = _neutralize(sql).strip()
    match = _LEADING_WORD.match(neutralized)
    return match.group(1).upper() if match else ""


def is_multi_statement(sql: str) -> bool:
    """True when a ';' separates statements. Semicolons inside string
    literals and comments don't count, and a single trailing ';' is fine."""
    neutralized = _neutralize(sql)
    return ";" in neutralized.strip().rstrip(";")


def keyword_after_ctes(sql: str) -> str:
    """Extract the top-level SQL keyword following CTE definitions.

    When a statement starts with WITH, this returns the keyword of the actual
    statement following the CTE list. For example:
    - "WITH x AS (SELECT 1) INSERT INTO ..." returns "INSERT"
    - "WITH x AS (SELECT 1) SELECT * FROM x" returns "SELECT"

    Handles comments, string literals (all types including backticks),
    RECURSIVE modifier, multiple CTEs, and column lists. Uses a tokenizer
    to safely parse paren-balanced CTEs against the neutralized version.
    """
    # Neutralize quotes and comments so they can't affect paren counting
    body = _neutralize(sql).strip()

    # Tokenize: extract words and structural tokens (parens, commas)
    tokens = []
    i = 0
    while i < len(body):
        # Skip whitespace
        while i < len(body) and body[i] in " \t\n\r":
            i += 1
        if i >= len(body):
            break

        # Extract words: letters, underscores, and digits
        if body[i].isalpha() or body[i] == "_":
            start = i
            while i < len(body) and (body[i].isalnum() or body[i] == "_"):
                i += 1
            tokens.append(body[start:i].upper())
        # Extract structural tokens
        elif body[i] in "(),":
            tokens.append(body[i])
            i += 1
        else:
            # Skip other characters (operators, etc.)
            i += 1

    # Verify it starts with WITH
    if not tokens or tokens[0] != "WITH":
        return ""

    pos = 1

    # Skip optional RECURSIVE keyword
    if pos < len(tokens) and tokens[pos] == "RECURSIVE":
        pos += 1

    # Parse the CTE list: each CTE is name [(cols)] AS (body)
    # Multiple CTEs are separated by commas
    while pos < len(tokens):
        # Pattern: NAME [ ( ... ) ] AS ( ... ) [, more CTEs]

        # Expect a name
        if pos >= len(tokens) or not tokens[pos][0].isalpha():
            break
        pos += 1

        # Skip optional column list (parens before AS)
        if pos < len(tokens) and tokens[pos] == "(":
            # Look ahead: if next closing ) is followed by AS, this is a column list
            save_pos = pos + 1
            depth = 1
            while save_pos < len(tokens) and depth > 0:
                if tokens[save_pos] == "(":
                    depth += 1
                elif tokens[save_pos] == ")":
                    depth -= 1
                save_pos += 1

            # Check if AS follows the closing paren
            if save_pos < len(tokens) and tokens[save_pos] == "AS":
                # Yes, it's a column list - skip it
                pos = save_pos
            # If not AS, this paren must be the CTE body, so don't skip

        # Expect AS
        if pos >= len(tokens) or tokens[pos] != "AS":
            # Malformed; return what we have
            if pos < len(tokens):
                return tokens[pos]
            return ""
        pos += 1

        # Expect and skip balanced parens (CTE body)
        if pos >= len(tokens) or tokens[pos] != "(":
            if pos < len(tokens):
                return tokens[pos]
            return ""

        depth = 1
        pos += 1
        while pos < len(tokens) and depth > 0:
            if tokens[pos] == "(":
                depth += 1
            elif tokens[pos] == ")":
                depth -= 1
            pos += 1

        # After closing paren of CTE body, check for comma
        if pos < len(tokens) and tokens[pos] == ",":
            pos += 1
            # Continue to parse next CTE
        else:
            # No comma - CTE list is done, next token is the statement keyword
            if pos < len(tokens):
                return tokens[pos]
            return ""

    return ""


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

    # CTE-prefixed DML detection: WITH keyword is allowed, but only if
    # the actual statement following the CTE definitions is SELECT
    if keyword == "WITH":
        actual_keyword = keyword_after_ctes(sql)
        if actual_keyword != "SELECT":
            raise SqlNotAllowed(
                f"CTE-prefixed DML is not permitted; got {actual_keyword or '?'}, "
                f"only SELECT is allowed after CTE definitions"
            )

    body = sql.strip().rstrip(";").rstrip()
    if keyword in {"SELECT", "WITH"} and not _TRAILING_LIMIT.search(
        strip_comments(body)
    ):
        body = f"{body}\nLIMIT {max_rows}"
    return body
