"""
Migration Runner MCP server.

Tool set:

  run_python(code, timeout_seconds=3600)
      Synchronous: block until the Python script exits, then return the
      full stdout/stderr/exit_code. Use for short scripts (~1 min or less).

  run_python_background(code, timeout_seconds=3600)
      Start a Python script in the background, return immediately with
      {job_id, pid}. Use for long migrations where you want progress
      chunks to appear in the chat as the script runs.

  tail_python_job(job_id, stdout_offset, stderr_offset, max_wait_seconds=60)
      Block up to `max_wait_seconds` waiting for new stdout/stderr from
      the given job, then return the delta + status + exit_code. Call in
      a loop, passing back the returned offsets, until status == "done".
      Each call's tool-result renders in the chat UI as a visible chunk
      of progress.

  list_workspace_files() / read_workspace_file(path) / write_workspace_file(path, content)
      Workspace helpers — unchanged from earlier versions.

Why two patterns: LibreChat v0.8.5's MCP integration does not surface
server-sent MCP `notifications/message` (log) events in the chat UI, so
in-band streaming over a single tool call is invisible to partners. The
background+tail pattern works around that by relying on per-tool-result
rendering — which LibreChat does display.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

WORKSPACE = Path("/workspace")
WORKSPACE.mkdir(exist_ok=True)

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))

mcp = FastMCP("migration-runner", host=HOST, port=PORT)


def _resolve_workspace_path(path: str) -> Path:
    candidate = (WORKSPACE / path).resolve()
    if not str(candidate).startswith(str(WORKSPACE)):
        raise ValueError(f"Path {path!r} escapes the workspace")
    return candidate


# ─────────────────────────────────────────────────────────────────────
# run_python — synchronous one-shot
# ─────────────────────────────────────────────────────────────────────


@mcp.tool()
def run_python(code: str, timeout_seconds: int = 3600) -> dict:
    """
    Execute a Python script inside the migration-runner container and
    block until it exits. Returns the full captured stdout/stderr.

    For long migrations, prefer `run_python_background` + `tail_python_job`
    so partners see progress chunks in the chat.

    timeout_seconds defaults to 3600 (1h).
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir="/tmp", delete=False
    ) as f:
        f.write(code)
        script_path = f.name

    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-u", script_path],
            cwd=str(WORKSPACE),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "stdout": (e.stdout.decode("utf-8", errors="replace") if e.stdout else ""),
            "stderr": (e.stderr.decode("utf-8", errors="replace") if e.stderr else "")
            + f"\n[migration-runner] Timed out after {timeout_seconds}s",
            "exit_code": -1,
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────
# run_python_background + tail_python_job — visible streaming
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _Job:
    proc: asyncio.subprocess.Process
    started_at: float
    timeout_seconds: int
    script_path: str
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


_jobs: dict[str, _Job] = {}


async def _drain(stream: asyncio.StreamReader, buffer: list[str]) -> None:
    while True:
        line = await stream.readline()
        if not line:
            return
        buffer.append(line.decode("utf-8", errors="replace").rstrip("\n"))


async def _supervise(job: _Job) -> None:
    """Drain the script's stdout/stderr, enforce the timeout, then mark
    the job done. Runs as a background task."""
    try:
        await asyncio.wait_for(
            asyncio.gather(
                _drain(job.proc.stdout, job.stdout_lines),
                _drain(job.proc.stderr, job.stderr_lines),
            ),
            timeout=job.timeout_seconds,
        )
        job.exit_code = await job.proc.wait()
    except asyncio.TimeoutError:
        job.proc.kill()
        try:
            await asyncio.wait_for(job.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        job.exit_code = -1
        job.stderr_lines.append(
            f"[migration-runner] Timed out after {job.timeout_seconds}s"
        )
    finally:
        if job.script_path:
            try:
                os.unlink(job.script_path)
            except OSError:
                pass
        job.done.set()


@mcp.tool()
async def run_python_background(code: str, timeout_seconds: int = 3600) -> dict:
    """
    Launch a Python script in the background. Returns immediately with
    `{job_id, status: "running", pid}`. Use for long migrations.

    Workflow:
      1. Call this once; capture the returned `job_id`.
      2. Call `tail_python_job(job_id, stdout_offset, stderr_offset)` in a
         loop, passing back the returned offsets. Each call returns the
         new chunk of stdout/stderr produced since the previous call.
      3. Stop when the response has `status == "done"`.

    Each `tail_python_job` call's result is rendered in the chat, so the
    partner sees migration progress chunks as they happen (typically one
    every ~60 seconds at default max_wait_seconds).

    timeout_seconds defaults to 3600 (1h).
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir="/tmp", delete=False
    ) as f:
        f.write(code)
        script_path = f.name

    # -u → unbuffered Python stdout/stderr so every print() reaches the
    # pipe immediately. Without it Python block-buffers when stdout is a
    # pipe and tail calls would see nothing until the buffer fills.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", script_path,
        cwd=str(WORKSPACE),
        env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    job_id = uuid.uuid4().hex[:12]
    job = _Job(
        proc=proc,
        started_at=time.monotonic(),
        timeout_seconds=timeout_seconds,
        script_path=script_path,
    )
    _jobs[job_id] = job
    asyncio.create_task(_supervise(job))

    return {
        "job_id": job_id,
        "status": "running",
        "pid": proc.pid,
    }


@mcp.tool()
async def tail_python_job(
    job_id: str,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
    max_wait_seconds: int = 120,
    min_chunk_seconds: int = 30,
) -> dict:
    """
    Return new stdout/stderr from a background job started with
    `run_python_background`.

    On the first call pass `stdout_offset=0, stderr_offset=0`. On each
    subsequent call pass back the offsets from the previous response —
    that's what makes each call return ONLY the new lines.

    Polling behaviour (tuned to keep the number of tool calls per
    migration bounded — each tool call costs one agent graph step):
      • If the job is already done, returns immediately with whatever's left.
      • If new output is available, keeps accumulating for at least
        `min_chunk_seconds` (default 30) so each poll returns a fat chunk
        rather than one line at a time.
      • If no new output yet, blocks up to `max_wait_seconds` (default 120)
        waiting for some.

    Returns:
      status            "running" or "done"
      exit_code         null while running; integer (0 = success) when done
      stdout_delta      string with new stdout lines (joined with newlines)
      stderr_delta      string with new stderr lines
      stdout_offset     pass back on the next call
      stderr_offset     pass back on the next call
      duration_seconds  total elapsed time since the job started
    """
    job = _jobs.get(job_id)
    if job is None:
        return {
            "error": f"unknown job_id {job_id!r}",
            "active_jobs": list(_jobs.keys()),
        }

    loop = asyncio.get_event_loop()
    wait_deadline = loop.time() + max(0, max_wait_seconds)
    first_data_time: float | None = None
    chunk_deadline: float | None = None

    while True:
        has_new = (
            len(job.stdout_lines) > stdout_offset
            or len(job.stderr_lines) > stderr_offset
        )
        if job.done.is_set():
            break
        if has_new:
            # Start the min-chunk accumulation window the first time we see data.
            if first_data_time is None:
                first_data_time = loop.time()
                chunk_deadline = first_data_time + max(0, min_chunk_seconds)
            # Keep accumulating until the chunk window closes.
            if chunk_deadline is not None and loop.time() >= chunk_deadline:
                break
        else:
            if loop.time() >= wait_deadline:
                break
        await asyncio.sleep(0.5)

    return {
        "status": "done" if job.done.is_set() else "running",
        "exit_code": job.exit_code,
        "stdout_delta": "\n".join(job.stdout_lines[stdout_offset:]),
        "stderr_delta": "\n".join(job.stderr_lines[stderr_offset:]),
        "stdout_offset": len(job.stdout_lines),
        "stderr_offset": len(job.stderr_lines),
        "duration_seconds": round(time.monotonic() - job.started_at, 2),
    }


# ─────────────────────────────────────────────────────────────────────
# Workspace helpers — unchanged
# ─────────────────────────────────────────────────────────────────────


@mcp.tool()
def list_workspace_files() -> list:
    """List files in /workspace (recursive, max depth 3)."""
    files = []
    for path in WORKSPACE.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(WORKSPACE)
        except ValueError:
            continue
        if len(rel.parts) > 3:
            continue
        try:
            files.append({"path": str(rel), "size_bytes": path.stat().st_size})
        except OSError:
            continue
    return files


@mcp.tool()
def read_workspace_file(path: str) -> str:
    """Read a file from /workspace and return its contents as text."""
    resolved = _resolve_workspace_path(path)
    if not resolved.is_file():
        raise ValueError(f"Not a file: {path}")
    return resolved.read_text(encoding="utf-8", errors="replace")


@mcp.tool()
def write_workspace_file(path: str, content: str) -> dict:
    """Write content to /workspace/<path>, creating parent dirs as needed."""
    resolved = _resolve_workspace_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return {
        "path": str(resolved.relative_to(WORKSPACE)),
        "size_bytes": len(content),
    }


if __name__ == "__main__":
    mcp.run(transport="sse")
