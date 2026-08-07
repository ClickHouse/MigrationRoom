"""
FastAPI HTTP layer over the migrationkit SQLite state store.

Runs inside the migration-runner MCP container in a daemon thread,
sharing /workspace/state/migrationkit.db with any user-script process
that imports migrationkit.

Routes:
  GET    /api/health
  GET    /api/runs
  GET    /api/runs/{run_id}
  GET    /api/runs/{run_id}/validations
  GET    /api/runs/{run_id}/benchmarks
  GET    /api/runs/{run_id}/events?since=N  (SSE)
  POST   /api/runs/{run_id}/pause
  POST   /api/runs/{run_id}/resume
  POST   /api/runs/{run_id}/cancel
  POST   /api/runs/{run_id}/mark/{step}     (step ∈ validated, benchmarked)
  DELETE /api/runs/{run_id}
  GET    /api/sources
  GET    /api/sources/{src}/databases
  GET    /api/sources/{src}/default-queries
  GET    /api/sources/{src}/prompts/{step}
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse

from . import state

SOURCES_DIR = Path(os.environ.get("MIGRATIONKIT_SOURCES_DIR", "/sources"))

# LibreChat MongoDB. Shared client across all helpers, lazy-init on
# first call. URL is the same hostname the LibreChat container uses
# (mongodb on the playground-net network).
_MONGO_URL = os.environ.get(
    "MONGODB_URL", "mongodb://mongodb:27017/LibreChat"
)
_mongo_client = None  # populated by _get_mongo_db()
_agent_id_cache: dict[str, str | None] = {}
_agent_id_cache_loaded = False
_playground_user_id_cache: str | None = None


def _get_mongo_db():
    """Cached MongoDB connection to the LibreChat database. Returns
    None if pymongo isn't installed or the server is unreachable;
    callers degrade gracefully (the dashboard just doesn't pre-select
    an agent or pre-create conversations)."""
    global _mongo_client
    if _mongo_client is not None:
        try:
            return _mongo_client.get_default_database()
        except Exception:
            _mongo_client = None
    try:
        from pymongo import MongoClient  # noqa: PLC0415

        _mongo_client = MongoClient(_MONGO_URL, serverSelectionTimeoutMS=2000)
        return _mongo_client.get_default_database()
    except Exception:
        return None


def _load_agent_ids_from_mongo() -> dict[str, str | None]:
    """Map `agent_name → agent_id` for every agent in LibreChat's
    Mongo. Cached for the process lifetime — if partners run
    `make reset-agent`, the migration-runner restart drops the cache."""
    global _agent_id_cache_loaded
    if _agent_id_cache_loaded:
        return _agent_id_cache
    db = _get_mongo_db()
    if db is None:
        return _agent_id_cache
    try:
        for doc in db.agents.find({}, {"name": 1, "id": 1, "_id": 0}):
            name = doc.get("name")
            agent_id = doc.get("id")
            if name and agent_id:
                _agent_id_cache[name] = agent_id
    except Exception:
        pass
    _agent_id_cache_loaded = True
    return _agent_id_cache


def _get_playground_user_id() -> str | None:
    """Look up the playground demo user's `_id` (stringified). Cached
    for the process lifetime — the user is created once by
    `librechat-init` and never replaced. Used as the `user` foreign-key
    on pre-created conversations so they show up under the right
    account in LibreChat."""
    global _playground_user_id_cache
    if _playground_user_id_cache is not None:
        return _playground_user_id_cache
    db = _get_mongo_db()
    if db is None:
        return None
    try:
        # Match either username or email — librechat-init uses both.
        u = db.users.find_one(
            {"$or": [
                {"username": "playground"},
                {"email": "admin@playground.local"},
            ]},
            {"_id": 1},
        )
        if u:
            _playground_user_id_cache = str(u["_id"])
    except Exception:
        pass
    return _playground_user_id_cache


def _read_source_manifest(src_dir: Path) -> dict:
    """Load `<source>/manifest.json` if present. Falls back to a
    sensible default so partners adding a source per docs/adding-a-source.md
    don't strictly have to write one (the UI is just nicer if they do).

    Also resolves `agent_id` from MongoDB if the manifest's `agent_name`
    matches a LibreChat agent — the dashboard uses this to pre-select
    the matching agent in the chat iframe."""
    manifest_path = src_dir / "manifest.json"
    data: dict = {}
    if manifest_path.is_file():
        try:
            with manifest_path.open() as f:
                data = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            data = {}
    label = data.get("label") or src_dir.name.replace("-", " ").replace("_", " ").title()
    env_var = data.get("default_database_env")
    fallback = data.get("default_database_fallback") or ""
    default_db = (env_var and os.environ.get(env_var)) or fallback
    agent_name = data.get("agent_name") or ""
    agent_id = _load_agent_ids_from_mongo().get(agent_name) if agent_name else None
    return {
        "id": src_dir.name,
        "label": label,
        "default_database": default_db,
        "agent_name": agent_name,
        "agent_id": agent_id,
    }

app = FastAPI(title="migrationkit", version="0.1.0")

# Same-origin via nginx in production; permissive here lets you curl during dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/runs")
def runs_list() -> list[dict]:
    return state.list_runs()


@app.get("/api/runs/{run_id}")
def runs_get(run_id: str) -> dict:
    run = state.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return run


@app.get("/api/runs/{run_id}/validations")
def runs_validations(run_id: str) -> list[dict]:
    """Per-table row-count comparisons recorded by `Validator.validate()`.
    Dashboard's Validation tab consumes this. Empty list when step 3
    hasn't been run yet (or was run on a different run_id)."""
    if not state.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return state.list_validations(run_id)


@app.get("/api/runs/{run_id}/benchmarks")
def runs_benchmarks(run_id: str) -> list[dict]:
    """Per-query timing comparisons recorded by `Benchmarker.benchmark()`.
    Dashboard's Benchmark tab consumes this. Empty list when step 4
    hasn't been run yet."""
    if not state.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return state.list_benchmarks(run_id)


@app.get("/api/runs/{run_id}/events")
async def runs_events(run_id: str, since: int = 0, request: Request = None) -> StreamingResponse:
    """SSE stream of events with id > since. Keeps the connection open
    and pushes new events as they appear. Disconnects when client closes
    or when the run reaches a terminal status."""

    async def gen():
        last = since
        idle = 0
        while True:
            if request is not None and await request.is_disconnected():
                return
            new = state.events_since(run_id, last)
            for ev in new:
                last = ev["id"]
                # Strip the raw payload_json column from the response — we
                # already parsed it into `payload`.
                payload = {
                    "id": ev["id"],
                    "ts": ev["ts"],
                    "kind": ev["kind"],
                    "payload": ev["payload"],
                }
                # IMPORTANT: do NOT set `event: <kind>` here. EventSource
                # dispatches typed events to `addEventListener("<kind>", …)`
                # listeners, not the default `onmessage` handler — and the
                # dashboard's subscriber uses onmessage. The kind is
                # already in the JSON payload so the client can branch on
                # it client-side. Adding the SSE `event:` field again
                # would silently break the sparkline + milestones.
                yield f"id: {ev['id']}\ndata: {json.dumps(payload)}\n\n"
            if new:
                idle = 0
            else:
                idle += 1
                # Heartbeat every ~15s of idle to keep proxies happy.
                if idle % 15 == 0:
                    yield ": heartbeat\n\n"
            # We deliberately do NOT auto-close on terminal run status.
            # Step 3 (Validate) and Step 5 (Benchmark) emit events
            # AFTER the run is "done", so closing here would force the
            # browser's EventSource to reconnect — and on the next
            # connection the server would just close again, looping
            # every ~3 s for the lifetime of the dashboard tab. Each
            # reconnect churns HTTP buffers + parser state; over a
            # multi-hour open dashboard that compounds into hundreds
            # of MB of client-side memory pressure across the three
            # EventSources the dashboard runs. The connection ends
            # naturally when the partner closes the dashboard tab,
            # which trips `request.is_disconnected()` above.
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/pause")
def runs_pause(run_id: str) -> dict:
    if not state.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    state.set_control_flag(run_id, "pause")
    return {"run_id": run_id, "flag": "pause"}


@app.post("/api/runs/{run_id}/resume")
def runs_resume(run_id: str) -> dict:
    if not state.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    state.set_control_flag(run_id, "run")
    return {"run_id": run_id, "flag": "run"}


@app.post("/api/runs/{run_id}/cancel")
def runs_cancel(run_id: str) -> dict:
    if not state.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    state.set_control_flag(run_id, "cancel")
    return {"run_id": run_id, "flag": "cancel"}


@app.post("/api/runs/{run_id}/mark/{step}")
async def runs_mark(run_id: str, step: str, request: Request) -> dict:
    """Step-completion marker for dashboard checkmarks. Called by the
    agent after step 3 (validated) and step 4 (benchmarked). Optional
    JSON body is recorded as the event payload (e.g. step-4 benchmark
    table)."""
    if step not in ("validated", "benchmarked"):
        raise HTTPException(status_code=400, detail=f"unknown step {step!r}")
    if not state.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    payload: dict[str, Any] = {}
    try:
        body = await request.body()
        if body:
            payload = json.loads(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}")
    event_id = state.write_event(run_id, f"step_{step}", payload)
    return {"run_id": run_id, "step": step, "event_id": event_id}


@app.delete("/api/runs/{run_id}")
def runs_delete(run_id: str) -> dict:
    try:
        ok = state.delete_run(run_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return {"run_id": run_id, "deleted": True}


@app.get("/api/sources")
def list_sources() -> list[dict]:
    """Discover migration sources mounted at /sources. Dashboard renders
    the source dropdown from this — adding a new source on disk shows up
    in the UI without rebuilding the dashboard."""
    if not SOURCES_DIR.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(SOURCES_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        # Treat a directory as a valid source if it has at least one of
        # the canonical subdirectories — keeps stray dirs (like `legacy/`)
        # from polluting the dropdown.
        if not (entry / "prompts").is_dir() and not (entry / "queries").is_dir():
            continue
        out.append(_read_source_manifest(entry))
    return out


# In-memory cache for the source-databases listing. Dashboard fetches
# this every time the source dropdown changes — without a cache, the
# 2–5 s Snowflake connection lights up every click. 5-min TTL keeps
# things fresh enough for partners adding databases mid-session.
_DB_CACHE: dict[str, tuple[float, list[str]]] = {}
_DB_CACHE_TTL_SEC = 300


@app.get("/api/sources/{src}/databases")
def list_source_databases(src: str, refresh: bool = False) -> list[str]:
    """Enumerate databases the source-side credentials can see. Backs
    the dashboard's source-database dropdown."""
    import time as _time

    if "/" in src or ".." in src:
        raise HTTPException(status_code=400, detail="invalid source name")

    if not refresh:
        cached = _DB_CACHE.get(src)
        if cached and _time.time() - cached[0] < _DB_CACHE_TTL_SEC:
            return cached[1]

    try:
        if src == "snowflake":
            from .sources.snowflake import SnowflakeSource
            dbs = SnowflakeSource.list_databases_from_env()
        elif src == "postgres":
            from .sources.postgres import PostgresSource
            dbs = PostgresSource.list_databases_from_env()
        elif src == "clickhouse-oss":
            from .sources.clickhouse_oss import ClickHouseOssSource
            dbs = ClickHouseOssSource.list_databases_from_env()
        elif src == "bigquery":
            from .sources.bigquery import BigQuerySource
            dbs = BigQuerySource.list_databases_from_env()
        elif src == "databricks":
            from .sources.databricks import DatabricksSource
            dbs = DatabricksSource.list_databases_from_env()
        else:
            raise HTTPException(
                status_code=404,
                detail=f"source {src!r} does not support database listing",
            )
    except KeyError as missing:
        # Credentials env var missing — caller falls back to text input.
        raise HTTPException(
            status_code=503,
            detail=f"source {src!r} credentials not configured (missing {missing})",
        )
    except Exception as e:
        # Connection failed (wrong creds, network, etc.) — surface to caller.
        raise HTTPException(
            status_code=502,
            detail=f"could not list databases on {src!r}: {e}",
        )

    _DB_CACHE[src] = (_time.time(), dbs)
    return dbs


@app.post("/api/sources/{src}/conversation")
def sources_conversation(src: str) -> dict:
    """Resolve a LibreChat conversation pre-bound to the source's agent.

    Reuses the playground user's most recent (non-archived) conversation
    for that agent if one exists; otherwise inserts a fresh conversation
    document in MongoDB and returns the new `conversationId`. The
    dashboard's `handleSourceChange` calls this so the chat iframe
    lands on `/c/<conversationId>` — LibreChat reads the persisted
    `agent_id` from the conversation and pre-selects the right agent.

    URL params (`?endpoint=agents&agent_id=...`) on `/c/new` are NOT
    honored by LibreChat v0.8.5 — only `redirect_uri` is read. The
    conversation-document path is the reliable mechanism."""
    import uuid as _uuid
    from datetime import datetime as _datetime

    if "/" in src or ".." in src:
        raise HTTPException(status_code=400, detail="invalid source name")
    src_dir = SOURCES_DIR / src
    if not src_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown source {src!r}")

    meta = _read_source_manifest(src_dir)
    agent_id = meta.get("agent_id")
    if not agent_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no agent registered for {src!r} — run `make reset-agent` "
                f"if the librechat-init container failed to create it."
            ),
        )

    db = _get_mongo_db()
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unreachable from migration-runner; can't "
            "create LibreChat conversation",
        )
    user_id = _get_playground_user_id()
    if not user_id:
        raise HTTPException(
            status_code=503,
            detail="playground user not found in MongoDB (librechat-init may "
            "not have run)",
        )

    # Reuse the most-recent non-archived conversation for this agent so
    # flipping sources doesn't spam the partner's chat history.
    latest = db.conversations.find_one(
        {"user": user_id, "agent_id": agent_id, "isArchived": False},
        sort=[("updatedAt", -1)],
    )
    if latest and latest.get("conversationId"):
        return {
            "conversation_id": latest["conversationId"],
            "agent_id": agent_id,
            "reused": True,
        }

    # Otherwise create a fresh empty conversation pre-bound to the agent.
    # LibreChat loads it cleanly even with no messages — the chat input
    # is ready and the partner can type immediately, with the right
    # agent already in the header.
    conv_id = str(_uuid.uuid4())
    now = _datetime.utcnow()
    db.conversations.insert_one({
        "user": user_id,
        "conversationId": conv_id,
        "agent_id": agent_id,
        "endpoint": "agents",
        "title": meta.get("agent_name") or f"{meta.get('label')} migration",
        "isArchived": False,
        "tags": [],
        "files": [],
        "createdAt": now,
        "updatedAt": now,
        "__v": 0,
    })
    return {
        "conversation_id": conv_id,
        "agent_id": agent_id,
        "reused": False,
    }


@app.get("/api/sources/{src}/default-queries", response_class=PlainTextResponse)
def sources_default_queries(src: str) -> str:
    """Return the contents of sources/<src>/queries/sample_olap_queries.sql.
    Used by the dashboard to populate the Analytical Queries dialog default."""
    if "/" in src or ".." in src:
        raise HTTPException(status_code=400, detail="invalid source name")
    path = SOURCES_DIR / src / "queries" / "sample_olap_queries.sql"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no default queries for {src!r}")
    return path.read_text(encoding="utf-8")


# Step ID → filename for the 6-step workflow. Each source's prompts/
# dir is expected to contain these six files. Steps 3 + 4 split the
# old "validate-and-rewrite" combined prompt; steps 5 + 6 split the
# old "benchmark-and-optimize" prompt — each new button drives one
# logical operation (mechanical library call vs in-chat reasoning),
# which keeps the agent's tool-call budget tight.
#
# Sources still carrying the legacy 7-prompt set or the older 4-step
# combined set will return 404 here until migrated.
_STEP_FILES = {
    "discover-and-design": "01-discover-and-design.md",
    "migrate-data":        "02-migrate-data.md",
    "validate":            "03-validate.md",
    "rewrite-queries":     "04-rewrite-queries.md",
    "benchmark":           "05-benchmark.md",
    "optimize":            "06-optimize.md",
}


@app.get("/api/sources/{src}/prompts/{step}", response_class=PlainTextResponse)
def sources_prompt(src: str, step: str) -> str:
    """Return the raw markdown for a step's prompt. Placeholders like
    `{source}`, `{database}`, `{olap_queries}` are NOT substituted here
    — the dashboard does that client-side using the pickers + dialog."""
    if "/" in src or ".." in src or step not in _STEP_FILES:
        raise HTTPException(status_code=400, detail="invalid src/step")
    path = SOURCES_DIR / src / "prompts" / _STEP_FILES[step]
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"prompt {step!r} not yet authored for source {src!r}",
        )
    return path.read_text(encoding="utf-8")


def serve(host: str = "0.0.0.0", port: int = 8001) -> None:
    """Block-run the API. Invoked from a daemon thread in server.py."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
