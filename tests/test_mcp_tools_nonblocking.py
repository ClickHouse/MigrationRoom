"""The databricks-source MCP tools must not block the event loop.

FastMCP 1.x dispatches tools with:

    if fn_is_async: return await fn(**args)
    else:           return fn(**args)          # ← on the event loop

There is no `to_thread` on the tool path (only for resources). So a *sync*
tool that does blocking network I/O — which every tool in this server does,
talking Thrift/HTTPS to a SQL warehouse — freezes the whole server for its
duration: no other tool call progresses, no ping is answered, no queued
response is flushed. Tool calls therefore serialize, and LibreChat's 60 s
per-call timeout is measured from when it *sent* the call, so calls waiting
their turn burn the budget and fail with MCP error -32001 even though each
one individually takes a few seconds.

These tests read the source rather than importing it: the local suite
deliberately depends only on pytest + sqlglot (see tests/requirements.txt),
not the MCP SDK. The invariant being guarded is a property of the source
shape, so that is a faithful way to check it.

Scope is databricks-mcp. `docker/migration-runner/server.py` has sync tools
with the same hazard, but that is a separate server with a 900 s client
timeout and is not what this test governs.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "docker" / "databricks-mcp" / "server.py"


def _is_mcp_tool(node: ast.AST) -> bool:
    """True if this function carries an `@mcp.tool(...)` decorator."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def _tools() -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(SERVER.read_text())
    return [n for n in ast.walk(tree) if _is_mcp_tool(n)]


def test_server_exposes_the_five_tools() -> None:
    """Guards the test itself: if the decorator scan silently matched
    nothing, every assertion below would vacuously pass."""
    names = sorted(t.name for t in _tools())
    assert names == [
        "describe_table",
        "list_catalogs",
        "list_schemas",
        "list_tables",
        "run_select_query",
    ]


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


@pytest.mark.parametrize("tool", _tools(), ids=lambda t: t.name)
def test_tool_does_not_block_the_event_loop(
    tool: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    """Each tool must keep its blocking work off the event loop.

    Two shapes satisfy that, and both are accepted: carrying the `@_threaded`
    decorator, which hands a sync body to a worker thread; or being `async
    def` and awaiting a thread offload directly. What fails is the shape this
    server originally had — a bare sync `def` that FastMCP calls inline.
    """
    if "_threaded" in _decorator_names(tool):
        return

    assert isinstance(tool, ast.AsyncFunctionDef), (
        f"{tool.name} is a bare sync `def`: FastMCP will call it on the event "
        f"loop and block every other request for its duration. Add @_threaded "
        f"or make it async with an explicit thread offload."
    )
    source = ast.unparse(tool)
    assert any(isinstance(n, ast.Await) for n in ast.walk(tool)) and (
        "to_thread" in source
    ), (
        f"{tool.name} is async but never offloads to a thread, so its "
        f"connector calls still run on the event loop"
    )


def test_threaded_decorator_actually_offloads() -> None:
    """Guards the escape hatch above: if `@_threaded` ever stopped being an
    async thread offload, every tool would silently start blocking again while
    the parametrized test kept passing."""
    tree = ast.parse(SERVER.read_text())
    fns = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_threaded"
    ]
    assert len(fns) == 1, "_threaded is missing — the tools rely on it"
    body = ast.unparse(fns[0])
    assert "async def" in body, "_threaded must return a coroutine function"
    assert "to_thread.run_sync" in body, "_threaded must offload to a thread"
    assert "functools.wraps" in body, (
        "_threaded must preserve __doc__ and the signature, or FastMCP will "
        "publish the wrong schema and description to the model"
    )
