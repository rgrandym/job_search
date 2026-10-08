"""The assistant's tools as an MCP server (JSON-RPC over streamable HTTP, JSON responses).

A native run (`runtime._run_native`) gives its model the URL `/api/mcp/<token>`; the model's
own agent loop lists and calls the tools there. Each call runs through the same `_run_tool`
as the app's own loop, so the UI sees the same tool events. A token is valid only while its
run is in progress.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.agents.runtime import RUNS, Cancelled, run_tool
from src.core.llm import ToolCall

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "jobsearch", "version": "1.0.0"}


def _reply(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def parse_error() -> dict[str, Any]:
    return _error(None, -32700, "Expected one JSON-RPC message")


async def handle(token: str, request: dict[str, Any]) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; None for notifications (no reply is sent)."""
    request_id, method = request.get("id"), str(request.get("method", ""))
    params = request.get("params") or {}
    if "id" not in request:
        return None
    run = RUNS.get(token)
    if run is None:
        return _error(request_id, -32001, "This assistant run has ended")
    if method == "initialize":
        return _reply(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    if method == "ping":
        return _reply(request_id, {})
    if method == "tools/list":
        specs = run.defn.tool_specs()
        tools = [
            {"name": t.name, "description": t.description, "inputSchema": t.parameters}
            for t in specs
        ]
        return _reply(request_id, {"tools": tools})
    if method == "tools/call":
        call = ToolCall(
            id=f"mcp_{uuid.uuid4().hex[:10]}",
            name=str(params.get("name", "")),
            arguments=params.get("arguments") or {},
        )
        try:
            result = await run_tool(call, run)
        except Cancelled:
            return _error(request_id, -32002, "The user stopped this run")
        return _reply(
            request_id,
            {"content": [{"type": "text", "text": result.content}], "isError": result.is_error},
        )
    return _error(request_id, -32601, f"Method not found: {method}")
