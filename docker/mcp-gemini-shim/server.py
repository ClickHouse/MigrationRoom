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
    MCP_ALLOWED_HOSTS  comma-separated Host header allow-list for the SSE
                       transport's DNS-rebinding protection. Defaults to
                       loopback only; every instance must add the compose
                       service name its clients dial (see docker-compose.yml),
                       because this image is generic and cannot know it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.transport_security import TransportSecuritySettings
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
    """Open a persistent client session to the upstream MCP.

    Retries with exponential backoff on transient failures. This makes
    the shim resilient to two failure modes:

    1. **Startup race**: the shim is brought up before the upstream
       MCP server has finished initialising (its FastAPI startup
       includes a Snowflake login round-trip). compose `depends_on`
       with `service_healthy` is the first line of defence; this
       retry loop is the safety net for environments where the
       healthcheck isn't configured.
    2. **Mid-run transient**: if the upstream restarts (image pull,
       crash, etc.), the shim's existing session breaks. The supervisor
       calls `_open_upstream` again after `_close_upstream`; without
       retries, a still-warming-up upstream would bounce the shim into
       its own crash loop.

    Cap is generous (~60 s total wait) so we don't keep a CI / dev
    machine spinning forever; uvicorn will exit and Docker will restart
    the container, which is the desired behaviour after that point."""
    global _upstream_session, _upstream_stack
    delays = [0.5, 1, 2, 4, 8, 15, 30]   # max ~60s of waiting
    last_error: Exception | None = None
    for attempt, delay in enumerate([0] + delays, start=1):
        if delay:
            log.warning(
                "Upstream connect failed (attempt %d/%d): %s — retrying in %ss",
                attempt - 1, len(delays) + 1, last_error, delay,
            )
            await asyncio.sleep(delay)
        try:
            log.info("Connecting to upstream MCP: %s", UPSTREAM_URL)
            stack = contextlib.AsyncExitStack()
            streams = await stack.enter_async_context(sse_client(UPSTREAM_URL))
            session = await stack.enter_async_context(
                ClientSession(streams[0], streams[1])
            )
            await session.initialize()
            tools = await session.list_tools()
            log.info("Upstream advertised %d tools", len(tools.tools))
            # Commit only on success — leave the previous state intact
            # if a retry attempt failed half-way through.
            _upstream_stack = stack
            _upstream_session = session
            return
        except Exception as e:
            last_error = e
            # Clean up the partial stack so we don't leak file handles
            # on retry.
            try:
                await stack.aclose()
            except Exception:
                pass

    raise RuntimeError(
        f"Could not connect to upstream MCP at {UPSTREAM_URL} after "
        f"{len(delays) + 1} attempts. Last error: {last_error}"
    )


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
# Without security_settings the SDK enforces DNS-rebinding protection against
# an EMPTY allow-list, so every Host header is rejected with 421 — including
# the compose service name LibreChat dials. The failure is silent from the
# healthcheck's point of view, since that probes localhost. Protection stays
# on (see docker/databricks-mcp/server.py for the reasoning); the allow-list
# is supplied per instance because this image is source-agnostic.
_ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*").split(",")
    if h.strip()
]

sse_transport = SseServerTransport(
    "/messages/",
    security_settings=TransportSecuritySettings(
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=[f"http://{h}" for h in _ALLOWED_HOSTS]
        + [f"https://{h}" for h in _ALLOWED_HOSTS],
    ),
)


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
