"""
MCP shim that proxies an upstream MCP server while sanitizing tool input
schemas to be compatible with Google Gemini's function-calling API.

Gemini's tools API uses a strict subset of OpenAPI 3.0 Schema. Many MCP
servers (e.g. snowflake-labs-mcp, mcp-server-fetch) emit tool schemas
containing JSON Schema fields like `exclusiveMaximum`, `$schema`, `const`,
or composition keywords (`allOf`, `oneOf`) that Gemini rejects with
HTTP 400 "Unknown name ...". This shim sits between LibreChat and the
upstream MCP, intercepts `tools/list`, and strips those fields.

References:
- Google Gemini Schema docs: https://ai.google.dev/api/caching#Schema
- modelcontextprotocol/servers#1624 (same root cause)
- pydantic/pydantic-ai#1250

Configuration via environment:
    UPSTREAM_MCP_URL   required — e.g. http://snowflake-source:8000/sse
    SHIM_NAME          MCP server name advertised to clients (default: gemini-shim)
    MCP_HOST           bind host (default: 0.0.0.0)
    MCP_PORT           bind port (default: 8000)
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Mount, Route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("mcp-gemini-shim")

UPSTREAM_URL = os.environ["UPSTREAM_MCP_URL"]
SHIM_NAME = os.environ.get("SHIM_NAME", "gemini-shim")
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))

# JSON Schema fields that Google Gemini's function-calling API does not
# accept. The supported set is documented at
# https://ai.google.dev/api/caching#Schema — anything outside it returns
# HTTP 400 with "Unknown name <field>".
DROP_FIELDS: set[str] = {
    # JSON Schema metadata
    "$schema", "$ref", "$id", "$defs", "definitions",
    # Numeric constraints not in Gemini's subset
    "exclusiveMaximum", "exclusiveMinimum", "multipleOf",
    # Value constraints
    "const",
    # Composition keywords (Gemini supports anyOf only)
    "allOf", "oneOf", "not",
    # Conditional
    "if", "then", "else",
    # Dependencies
    "dependencies", "dependentRequired", "dependentSchemas",
    # Array
    "contains", "minContains", "maxContains", "uniqueItems",
    "additionalItems", "prefixItems", "unevaluatedItems",
    # Object
    "patternProperties", "unevaluatedProperties",
    # Content
    "contentEncoding", "contentMediaType", "contentSchema",
    # Annotation
    "readOnly", "writeOnly", "examples",
}


def sanitize_schema(node: Any) -> Any:
    """Recursively drop Gemini-incompatible fields from a JSON Schema."""
    if isinstance(node, dict):
        return {
            k: sanitize_schema(v)
            for k, v in node.items()
            if k not in DROP_FIELDS
        }
    if isinstance(node, list):
        return [sanitize_schema(item) for item in node]
    return node


# ── Upstream MCP session (opened at startup, shared across requests) ──
_upstream_session: ClientSession | None = None
_upstream_stack: contextlib.AsyncExitStack | None = None


async def _open_upstream() -> None:
    """Open a persistent client session to the upstream MCP."""
    global _upstream_session, _upstream_stack
    log.info("Connecting to upstream MCP: %s", UPSTREAM_URL)
    _upstream_stack = contextlib.AsyncExitStack()
    streams = await _upstream_stack.enter_async_context(sse_client(UPSTREAM_URL))
    _upstream_session = await _upstream_stack.enter_async_context(
        ClientSession(streams[0], streams[1])
    )
    await _upstream_session.initialize()
    tools = await _upstream_session.list_tools()
    log.info("Upstream advertised %d tools", len(tools.tools))


async def _close_upstream() -> None:
    global _upstream_stack
    if _upstream_stack is not None:
        await _upstream_stack.aclose()
        _upstream_stack = None


# ── Build the shim MCP server ─────────────────────────────────────────
server: Server = Server(SHIM_NAME)


@server.list_tools()
async def list_tools() -> list[Tool]:
    assert _upstream_session is not None, "upstream session not initialized"
    upstream = await _upstream_session.list_tools()
    sanitized: list[Tool] = []
    for tool in upstream.tools:
        sanitized.append(
            Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=sanitize_schema(tool.inputSchema),
            )
        )
    log.debug("Forwarded tools/list (%d tools)", len(sanitized))
    return sanitized


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    assert _upstream_session is not None, "upstream session not initialized"
    log.debug("Proxying tools/call name=%s", name)
    result = await _upstream_session.call_tool(name, arguments)
    return result.content


# ── SSE transport via Starlette ───────────────────────────────────────
sse_transport = SseServerTransport("/messages/")


async def handle_sse(request) -> None:
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options(),
        )


@contextlib.asynccontextmanager
async def lifespan(app):
    await _open_upstream()
    try:
        yield
    finally:
        await _close_upstream()


app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse_transport.handle_post_message),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
