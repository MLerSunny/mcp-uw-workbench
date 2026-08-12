"""Agent-to-agent transport layer.

The orchestrator in `underwriting-mcp` needs to invoke tools that live in
*other* MCP servers (`risk-mcp`, `pricing-mcp`). This module is the seam
where that happens.

Two implementations satisfy the same `ToolGateway` protocol:

* `MCPGateway`  - the real thing. Spawns each peer server as a subprocess
  and speaks MCP over stdio. Cross-process, cross-network-capable, and the
  only contract between orchestrator and peers is the MCP tool schema.

* `DirectGateway` - in-process function calls. Used by unit tests so the
  suite does not pay subprocess startup cost on every assertion. It is a
  test double, *not* the production path.

Keeping both behind one protocol means the orchestrator has no idea which
it is talking to, and swapping transports (stdio -> SSE -> streamable
HTTP) touches only this file. See docs/ADRs/0002-gateway-and-transport.md.
"""

from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from typing import Any, Protocol, Self, runtime_checkable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Logical peer names used by the orchestrator.
RISK = "risk"
PRICING = "pricing"


@runtime_checkable
class ToolGateway(Protocol):
    """Anything the orchestrator can delegate a tool call through."""

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        ...


# --------------------------------------------------------------------------
# Real MCP transport
# --------------------------------------------------------------------------

def _module_server(module: str) -> StdioServerParameters:
    """Launch a peer server via `python -m <module>`.

    Deliberately uses `sys.executable` rather than the console-script name
    (`risk-mcp`) so the gateway works from a venv, a container, or a test
    runner without depending on PATH.
    """
    return StdioServerParameters(command=sys.executable, args=["-m", module])


DEFAULT_PEERS: dict[str, StdioServerParameters] = {
    RISK: _module_server("mcp_uw_workbench.risk.server"),
    PRICING: _module_server("mcp_uw_workbench.pricing.server"),
}


def _unwrap(result: Any) -> dict[str, Any]:
    """Turn an MCP CallToolResult into the dict the orchestrator expects.

    Prefers `structured_content` (populated when a tool returns a dict and
    the server advertises structured output). Falls back to parsing the
    first text block, which is what older/plain servers emit.
    """
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        # Servers may wrap a bare return value under a synthetic key.
        if set(structured.keys()) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured

    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    return {"error": "Could not decode tool result", "raw": repr(result)[:400]}


class MCPGateway:
    """Talks to peer agents over MCP stdio. Use as an async context manager.

        async with MCPGateway() as gw:
            risk = await gw.call("risk", "score_risk", {...})
    """

    def __init__(self, peers: dict[str, StdioServerParameters] | None = None) -> None:
        self._peers = peers if peers is not None else DEFAULT_PEERS
        self._sessions: dict[str, ClientSession] = {}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        stack = AsyncExitStack()
        await stack.__aenter__()
        self._stack = stack
        try:
            for name, params in self._peers.items():
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
        except BaseException:
            await stack.aclose()
            self._stack = None
            self._sessions.clear()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._sessions.clear()
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        session = self._sessions.get(server)
        if session is None:
            raise RuntimeError(
                f"No MCP session for peer {server!r}. "
                "Enter the MCPGateway context before calling."
            )
        result = await session.call_tool(tool, args)
        return _unwrap(result)

    async def list_peer_tools(self) -> dict[str, list[str]]:
        """Introspect what each peer advertises. Used by the eval harness."""
        out: dict[str, list[str]] = {}
        for name, session in self._sessions.items():
            listing = await session.list_tools()
            out[name] = [t.name for t in listing.tools]
        return out


# --------------------------------------------------------------------------
# In-process test double
# --------------------------------------------------------------------------

class DirectGateway:
    """Calls peer tool functions in-process. Test double only.

    Records every call so tests can assert on delegation order - which is
    the behaviour that actually matters for an orchestrator.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        from mcp_uw_workbench.pricing import server as pricing_srv
        from mcp_uw_workbench.risk import server as risk_srv

        self.calls.append((server, tool, args))

        module = {RISK: risk_srv, PRICING: pricing_srv}.get(server)
        if module is None:
            raise KeyError(f"Unknown peer: {server}")

        fn = getattr(module, tool, None)
        if fn is None:
            raise KeyError(f"Peer {server} has no tool {tool}")

        return fn(**args)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None
