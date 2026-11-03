"""The mcp-meta server: two tools in front of many MCP servers."""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .config import ServerSpec
from .rpc import Session, UpstreamError

PROTOCOL_VERSION = "2025-06-18"
TOOLS_CACHE_TTL = 300.0


def _type_label(schema: Dict[str, Any]) -> str:
    kind = schema.get("type")
    if kind == "array":
        items = schema.get("items") or {}
        return f"{items.get('type', 'any')}[]"
    if isinstance(kind, list):
        return "|".join(str(k) for k in kind)
    return str(kind or "any")


def format_signature(tool: Dict[str, Any]) -> str:
    """Compact one-line signature, e.g. ``google_serp(q: str, [limit: int])``."""
    schema = tool.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts: List[str] = []
    if isinstance(properties, dict):
        for name, spec in properties.items():
            spec = spec if isinstance(spec, dict) else {}
            label = f"{name}: {_type_label(spec)}"
            parts.append(label if name in required else f"[{label}]")
    return f"{tool.get('name')}({', '.join(parts)})"


def first_sentence(text: Any, limit: int = 140) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    cut = cleaned.find(". ")
    if cut > 10:
        cleaned = cleaned[: cut + 1]
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "..."


class MetaServer:
    def __init__(self, specs: Dict[str, ServerSpec]) -> None:
        self.specs = specs
        self._sessions: Dict[str, Session] = {}
        self._tools: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    # -- upstream access -------------------------------------------------
    def _session(self, name: str) -> Session:
        session = self._sessions.get(name)
        if session is None:
            session = Session(self.specs[name])
            self._sessions[name] = session
        return session

    def tools_for(self, name: str, refresh: bool = False) -> List[Dict[str, Any]]:
        cached = self._tools.get(name)
        if cached and not refresh and time.time() - cached[0] < TOOLS_CACHE_TTL:
            return cached[1]
        spec = self.specs[name]
        tools = [
            tool
            for tool in self._session(name).list_tools()
            if spec.allow_tool(str(tool.get("name")))
        ]
        self._tools[name] = (time.time(), tools)
        return tools

    # -- tool implementations --------------------------------------------
    def tool_mcp_tools(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        name = args.get("server")
        refresh = bool(args.get("refresh"))
        if not name:
            return self._server_overview(), False
        name = str(name)
        if name not in self.specs:
            return self._unknown_server(name), True
        try:
            tools = self.tools_for(name, refresh=refresh)
        except UpstreamError as exc:
            return str(exc), True
        lines = [f"{name}: {len(tools)} tools"]
        server_info = self._sessions[name].server_info
        if server_info.get("version"):
            lines[0] += f" (v{server_info['version']})"
        lines.append("")
        for tool in tools:
            lines.append(f"- {format_signature(tool)}")
            summary = first_sentence(tool.get("description"))
            if summary:
                lines.append(f"    {summary}")
        lines.append("")
        lines.append(
            f'Call: mcp_call server="{name}" tool="<name>" arguments={{...}}'
        )
        return "\n".join(lines), False

    def tool_mcp_call(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        name = args.get("server")
        tool = args.get("tool")
        if not name or not tool:
            return "mcp_call needs 'server' and 'tool'", True
        name, tool = str(name), str(tool)
        if name not in self.specs:
            return self._unknown_server(name), True
        if not self.specs[name].allow_tool(tool):
            return f"tool '{tool}' is filtered out for server '{name}'", True
        arguments = args.get("arguments") or {}
        if not isinstance(arguments, dict):
            return "'arguments' must be a JSON object", True
        try:
            result = self._session(name).call_tool(tool, arguments)
        except UpstreamError as exc:
            return str(exc), True
        parts: List[str] = []
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        for key in ("structuredContent",):
            if result.get(key) and not parts:
                parts.append(json.dumps(result[key], ensure_ascii=False))
        text = "\n".join(parts) or json.dumps(result, ensure_ascii=False)
        return text, bool(result.get("isError"))

    def _server_overview(self) -> str:
        lines = [f"{len(self.specs)} upstream servers", ""]
        for name in sorted(self.specs):
            spec = self.specs[name]
            target = spec.url or f"{spec.command} {' '.join(spec.args)}".strip()
            try:
                count: Any = f"{len(self.tools_for(name))} tools"
            except UpstreamError as exc:
                count = f"unavailable: {first_sentence(exc, 80)}"
            lines.append(f"- {name} [{spec.transport}] {count} <- {target}")
        lines.append("")
        lines.append('Inspect one: mcp_tools server="<name>"')
        return "\n".join(lines)

    def _unknown_server(self, name: str) -> str:
        return f"Unknown server '{name}'. Available: {', '.join(sorted(self.specs))}"

    # -- MCP protocol ----------------------------------------------------
    def tools_spec(self) -> List[Dict[str, Any]]:
        names = ", ".join(sorted(self.specs))
        return [
            {
                "name": "mcp_tools",
                "description": (
                    "List MCP servers, or the tools one server offers with "
                    "compact signatures. Available servers: " + names
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "server": {
                            "type": "string",
                            "description": "Server name. Omit for an overview.",
                        },
                        "refresh": {
                            "type": "boolean",
                            "description": "Bypass the tool-list cache.",
                        },
                    },
                },
            },
            {
                "name": "mcp_call",
                "description": (
                    "Call one tool on an MCP server. Pass the server, the tool "
                    "name, and its arguments object. Use mcp_tools first when "
                    "the tool's parameter names are unknown."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "tool": {"type": "string"},
                        "arguments": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["server", "tool"],
                },
            },
        ]

    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "mcp-meta", "version": __version__},
                },
            )
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": self.tools_spec()})
        if method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "mcp_tools":
                text, is_error = self.tool_mcp_tools(args)
            elif name == "mcp_call":
                text, is_error = self.tool_mcp_call(args)
            else:
                return self._error(
                    request_id, -32602, f"unknown tool '{name}'"
                )
            return self._result(
                request_id,
                {"content": [{"type": "text", "text": text}], "isError": is_error},
            )
        if request_id is None:
            return None
        return self._error(request_id, -32601, f"method not found: {method}")

    @staticmethod
    def _result(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()


def serve(specs: Dict[str, ServerSpec], stream: Any = None) -> int:
    server = MetaServer(specs)
    source = stream or sys.stdin
    try:
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(request, dict):
                continue
            response = server.handle(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        server.close()
    return 0
