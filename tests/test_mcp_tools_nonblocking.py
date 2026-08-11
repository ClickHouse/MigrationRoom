"""No MCP tool in this repo's own servers may block the event loop.

FastMCP 1.x dispatches tools with:

    if fn_is_async: return await fn(**args)
    else:           return fn(**args)          # ← on the event loop

There is no `to_thread` on the tool path (only for resources). So a *sync*
tool freezes the entire server for as long as it runs: no other tool call
progresses, no ping is answered, and responses already computed sit
unflushed. Tool calls serialize, and because a client's per-call timeout is
measured from when it *sent* the call, calls waiting their turn burn the
budget and fail — while the server logs nothing wrong, having answered each
one promptly.

This bit both of our hand-written servers:

- `databricks-mcp` — every tool does blocking Thrift/HTTPS I/O to a SQL
  warehouse. Four concurrent `describe_table` calls returned at 4.4 s,
  9.1 s, 13.4 s and 17.5 s (a perfect staircase) and a ping issued during a
  call took 4.03 s against 0.00 s idle. Real timeouts followed at
  LibreChat's 60 s limit.
- `migration-runner` — `run_python` blocks until its child exits, up to its
  3600 s default, which also starved `tail_python_job`, the very tool meant
  to report progress while a script runs.

What these tests can and cannot prove: they read the source with `ast`
rather than importing it, because the local suite deliberately depends only
on pytest + sqlglot (see tests/requirements.txt) and not the MCP SDK. A bare
sync tool is statically detectable and is what regresses in practice. An
`async def` tool that performs blocking I/O inline would block just as
badly, and that is *not* statically detectable — the async tools here use
asyncio subprocess APIs, which is a matter of review, not of this test.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "docker"

SERVERS = {
    "databricks-mcp": _ROOT / "databricks-mcp" / "server.py",
    "migration-runner": _ROOT / "migration-runner" / "server.py",
}

# Every tool each server exposes. Adding a tool means adding it here, which is
# deliberate: the new tool then has to satisfy the non-blocking check below.
EXPECTED_TOOLS = {
    "databricks-mcp": {
        "describe_table",
        "list_catalogs",
        "list_schemas",
        "list_tables",
        "run_select_query",
    },
    "migration-runner": {
        "list_workspace_files",
        "read_workspace_file",
        "run_python",
        "run_python_background",
        "tail_python_job",
        "write_workspace_file",
    },
}

_Func = ast.FunctionDef | ast.AsyncFunctionDef


def _decorator_names(node: _Func) -> set[str]:
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _tools(server: str) -> list[_Func]:
    tree = ast.parse(SERVERS[server].read_text())
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "tool" in _decorator_names(n)
    ]


def _all_tools() -> list[tuple[str, _Func]]:
    return [(s, t) for s in SERVERS for t in _tools(s)]


@pytest.mark.parametrize("server", sorted(SERVERS))
def test_tool_scan_finds_every_tool(server: str) -> None:
    """Guards the tests themselves: if the decorator scan silently matched
    nothing, every assertion below would vacuously pass."""
    assert {t.name for t in _tools(server)} == EXPECTED_TOOLS[server]


@pytest.mark.parametrize(
    ("server", "tool"), _all_tools(), ids=lambda v: v if isinstance(v, str) else v.name
)
def test_tool_does_not_block_the_event_loop(server: str, tool: _Func) -> None:
    """A tool must not be a bare sync `def`.

    Two shapes pass: a sync body carrying `@_threaded`, which hands it to a
    worker thread; or an `async def`, which FastMCP awaits (its body is then
    expected to use async I/O — see this module's docstring).
    """
    if "_threaded" in _decorator_names(tool):
        return
    assert isinstance(tool, ast.AsyncFunctionDef), (
        f"{server}: {tool.name} is a bare sync `def`, so FastMCP will call it "
        f"on the event loop and block every other request for its duration. "
        f"Add @_threaded, or make it async over non-blocking I/O."
    )


@pytest.mark.parametrize("server", sorted(SERVERS))
def test_threaded_decorator_actually_offloads(server: str) -> None:
    """Guards the escape hatch above: if `@_threaded` ever stopped being an
    async thread offload, its tools would silently start blocking again while
    the check above kept passing."""
    tree = ast.parse(SERVERS[server].read_text())
    fns = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_threaded"
    ]
    users = [t.name for t in _tools(server) if "_threaded" in _decorator_names(t)]
    if not users:
        pytest.skip(f"{server} defines no @_threaded tools")

    assert len(fns) == 1, f"{server}: tools use @_threaded but it is not defined"
    body = ast.unparse(fns[0])
    assert "async def" in body, f"{server}: _threaded must return a coroutine function"
    assert "to_thread.run_sync" in body, f"{server}: _threaded must offload to a thread"
    assert "functools.wraps" in body, (
        f"{server}: _threaded must preserve __doc__ and the signature, or "
        f"FastMCP will publish the wrong schema and description to the model"
    )
