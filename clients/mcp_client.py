"""Minimal MCP streamable-HTTP client for the Superset MCP sidecar.

The sidecar runs stateless (`MCP_STATELESS_HTTP = True`), so every JSON-RPC
call is a plain POST and no session header has to be carried between calls.
Responses come back as a one-event SSE stream.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

MCP_URL = os.environ.get("SUPERSET_MCP_URL", "http://localhost:5008/mcp")
PROTOCOL_VERSION = "2025-06-18"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": PROTOCOL_VERSION,
}


class MCPError(RuntimeError):
    pass


def _parse_sse(text: str, request_id: int) -> dict[str, Any]:
    """Return the JSON-RPC response for `request_id`, skipping notifications."""
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = json.loads(line[5:].strip())
        if payload.get("id") == request_id:
            return payload
    raise MCPError(f"no JSON-RPC response in stream: {text[:200]}")


class MCPClient:
    def __init__(self, url: str = MCP_URL) -> None:
        self.url = url
        self._id = 0

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        response = requests.post(
            self.url,
            headers=_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": self._id,
                "method": method,
                "params": params or {},
            },
            timeout=120,
        )
        response.raise_for_status()
        payload = _parse_sse(response.text, self._id)
        if "error" in payload:
            raise MCPError(json.dumps(payload["error"]))
        return payload["result"]

    def initialize(self) -> dict[str, Any]:
        return self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "runtime-repair-scenarios", "version": "1"},
            },
        )

    def list_tools(self) -> list[str]:
        return [tool["name"] for tool in self._call("tools/list").get("tools", [])]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        if result.get("structuredContent"):
            return result["structuredContent"]
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    return {"text": block["text"], "isError": result.get("isError")}
        return result
