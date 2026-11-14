"""The mcp-meta server: two tools in front of many MCP servers."""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from . import __version__
from .config import ServerSpec
from .rpc import Session, UpstreamError

PROTOCOL_VERSION = "2025-06-18"
TOOLS_CACHE_TTL = 300.0
MAX_PARALLEL_SERVERS = 8
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200


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
    def __init__(
        self,
        specs: Dict[str, ServerSpec],
        reload: Optional[Callable[[], Tuple[Dict[str, ServerSpec], str]]] = None,
    ) -> None:
        self.specs = specs
        # Optional re-read of the config file. The server list is otherwise fixed
        # at startup, and Magpie rewrites library.json when connectors change, so
        # without this a name can leave the file while the process keeps the old
        # list (2026-11-13: three retries against a server that had just gone).
        self._reload = reload
        self._sessions: Dict[str, Session] = {}
        self._tools: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._state_lock = threading.RLock()
        self._tools_lock = threading.RLock()
        self._tool_locks = {name: threading.Lock() for name in specs}
        self._active: Dict[str, int] = {name: 0 for name in specs}
        self._last_used: Dict[str, float] = {}
        self._closed = False
        self._reaper_stop = threading.Event()
        self._reaper: Optional[threading.Thread] = None

        idle_timeouts = [
            spec.idle_timeout
            for spec in specs.values()
            if spec.idle_timeout is not None
        ]
        if idle_timeouts:
            interval = max(0.05, min(min(idle_timeouts) / 2.0, 30.0))
            self._reaper = threading.Thread(
                target=self._reaper_loop,
                args=(interval,),
                name="mcp-meta-idle-reaper",
                daemon=True,
            )
            self._reaper.start()

    # -- upstream access -------------------------------------------------
    @contextmanager
    def _session_use(self, name: str):
        with self._state_lock:
            if self._closed:
                raise UpstreamError("mcp-meta is shutting down")
            session = self._sessions.get(name)
            if session is None:
                session = Session(self.specs[name])
                self._sessions[name] = session
            self._active[name] = self._active.get(name, 0) + 1
            self._last_used[name] = time.monotonic()
        try:
            yield session
        finally:
            with self._state_lock:
                self._active[name] = max(0, self._active.get(name, 1) - 1)
                self._last_used[name] = time.monotonic()

    def tools_for(self, name: str, refresh: bool = False) -> List[Dict[str, Any]]:
        with self._tool_locks[name]:
            with self._tools_lock:
                cached = self._tools.get(name)
                if (
                    cached
                    and not refresh
                    and time.time() - cached[0] < TOOLS_CACHE_TTL
                ):
                    return cached[1]
            spec = self.specs[name]
            with self._session_use(name) as session:
                tools = [
                    tool
                    for tool in session.list_tools()
                    if spec.allow_tool(str(tool.get("name")))
                ]
            with self._tools_lock:
                self._tools[name] = (time.time(), tools)
            return tools

    def _prefetch(
        self, names: List[str], refresh: bool = False
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
        tools_by_name: Dict[str, List[Dict[str, Any]]] = {}
        errors: Dict[str, str] = {}
        if not names:
            return tools_by_name, errors
        if len(names) == 1:
            name = names[0]
            try:
                tools_by_name[name] = self.tools_for(name, refresh=refresh)
            except UpstreamError as exc:
                errors[name] = str(exc)
            return tools_by_name, errors

        workers = min(MAX_PARALLEL_SERVERS, len(names))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="mcp-meta-prefetch"
        ) as executor:
            futures = {
                executor.submit(self.tools_for, name, refresh): name
                for name in names
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    tools_by_name[name] = future.result()
                except UpstreamError as exc:
                    errors[name] = str(exc)
        return tools_by_name, errors

    def _server_version(self, name: str) -> str:
        with self._state_lock:
            session = self._sessions.get(name)
            if session is None:
                return ""
            return str(session.server_info.get("version") or "")

    def _format_tools(
        self, name: str, tools: List[Dict[str, Any]], heading: Optional[str] = None
    ) -> List[str]:
        lines = [heading or f"{name}: {len(tools)} tools"]
        version = self._server_version(name)
        if version and heading is None:
            lines[0] += f" (v{version})"
        for tool in tools:
            lines.append(f"- {format_signature(tool)}")
            summary = first_sentence(tool.get("description"))
            if summary:
                lines.append(f"    {summary}")
        return lines

    @staticmethod
    def _with_note(text: str, note: Optional[str]) -> str:
        """Put a config re-read note in front of the answer, when there is one."""
        if not note:
            return text
        return f"{note}.\n\n{text}"

    def _refresh_specs(self) -> Optional[str]:
        """Re-read the config file and adopt it. Returns a note, or None."""
        if self._reload is None:
            return None
        try:
            specs, source = self._reload()
        except Exception as exc:  # a broken file must not break the session
            return f"could not re-read the config: {exc}"
        with self._state_lock:
            if self._closed:
                return None
            added = [name for name in specs if name not in self.specs]
            removed = [name for name in self.specs if name not in specs]
            gone = [self._sessions.pop(name) for name in removed if name in self._sessions]
            for name in removed:
                self._tool_locks.pop(name, None)
                self._active.pop(name, None)
                self._last_used.pop(name, None)
            self.specs = specs
            self._tool_locks.update(
                {name: threading.Lock() for name in added}
            )
            self._active.update({name: 0 for name in added})
            with self._tools_lock:
                for name in removed:
                    self._tools.pop(name, None)
        for session in gone:
            session.close()
        if not added and not removed:
            return f"re-read {source}; the server list is unchanged ({len(specs)} servers)"
        bits = []
        if added:
            bits.append(f"added: {', '.join(sorted(added))}")
        if removed:
            bits.append(f"removed: {', '.join(sorted(removed))}")
        return f"re-read {source} ({len(specs)} servers; {'; '.join(bits)})"

    def _known_names(self, names: List[str]) -> Optional[str]:
        unknown = [name for name in names if name not in self.specs]
        if not unknown:
            return None
        return self._unknown_server(unknown)

    # -- tool implementations --------------------------------------------
    def tool_mcp_tools(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        name = args.get("server")
        tool_name = args.get("tool")
        query = args.get("query")
        servers_arg = args.get("servers")
        refresh = bool(args.get("refresh"))

        note: Optional[str] = None
        if tool_name is not None:
            if not name:
                return "'tool' requires a 'server'", True
            name = str(name)
            if name not in self.specs:
                note = self._refresh_specs()
                if name not in self.specs:
                    return self._with_note(self._unknown_server(name), note), True
            try:
                tools = self.tools_for(name, refresh=refresh)
            except UpstreamError as exc:
                return str(exc), True
            wanted = str(tool_name)
            for tool in tools:
                if tool.get("name") == wanted:
                    return self._with_note(
                        json.dumps(tool, ensure_ascii=False, indent=2), note
                    ), False
            available = ", ".join(str(tool.get("name")) for tool in tools)
            return (
                f"Unknown tool '{wanted}' on server '{name}'. "
                f"Available: {available}"
            ), True

        if name and servers_arg is not None:
            return "pass either 'server' or 'servers', not both", True

        names: Optional[List[str]] = None
        if name:
            names = [str(name)]
        elif servers_arg is not None:
            if not isinstance(servers_arg, list) or not servers_arg:
                return "'servers' must be a non-empty array of server names", True
            names = []
            for item in servers_arg:
                if not isinstance(item, str) or not item:
                    return "'servers' must contain non-empty strings", True
                if item not in names:
                    names.append(item)

        if names is not None:
            unknown = self._known_names(names)
            if unknown:
                note = self._refresh_specs()
                unknown = self._known_names(names)
                if unknown:
                    return self._with_note(unknown, note), True

        if query is not None:
            if not isinstance(query, str) or not query.strip():
                return "'query' must be a non-empty string", True
            limit = args.get("limit", DEFAULT_SEARCH_LIMIT)
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
                or limit > MAX_SEARCH_LIMIT
            ):
                return (
                    f"'limit' must be an integer from 1 to {MAX_SEARCH_LIMIT}"
                ), True
            return self._search_tools(
                names or sorted(self.specs), query, refresh=refresh, limit=limit
            )

        if names is not None:
            return self._with_note(
                self._list_servers(names, refresh=refresh)[0], note
            ), False
        # No arguments is also the documented way to pick up a rewritten config.
        note = self._refresh_specs()
        return self._with_note(self._server_overview(refresh=refresh), note), False

    def _list_servers(
        self, names: List[str], refresh: bool = False
    ) -> Tuple[str, bool]:
        tools_by_name, errors = self._prefetch(names, refresh=refresh)
        if len(names) == 1:
            name = names[0]
            if name in errors:
                return errors[name], True
            lines = self._format_tools(name, tools_by_name[name])
            lines.append("")
            lines.append(
                f'Call: mcp_call server="{name}" tool="<name>" arguments={{...}}'
            )
            return "\n".join(lines), False

        lines = [f"{len(names)} selected servers", ""]
        for name in names:
            if name in errors:
                lines.append(
                    f"- {name}: unavailable: {first_sentence(errors[name], 100)}"
                )
                continue
            lines.extend(self._format_tools(name, tools_by_name.get(name, [])))
            lines.append("")
        lines.append('Inspect a tool: mcp_tools server="<name>" tool="<tool>"')
        return "\n".join(lines).rstrip(), False

    def _search_tools(
        self, names: List[str], query: str, refresh: bool, limit: int
    ) -> Tuple[str, bool]:
        tools_by_name, errors = self._prefetch(names, refresh=refresh)
        terms = query.casefold().split()
        matches: Dict[str, List[Dict[str, Any]]] = {}
        total = 0
        shown = 0
        for name in names:
            for tool in tools_by_name.get(name, []):
                haystack = " ".join(
                    (
                        name,
                        str(tool.get("name") or ""),
                        str(tool.get("description") or ""),
                    )
                ).casefold()
                if all(term in haystack for term in terms):
                    total += 1
                    if shown < limit:
                        matches.setdefault(name, []).append(tool)
                        shown += 1

        if not total:
            lines = [f'No tools matched "{query}".']
            if errors:
                lines.append("")
                lines.append("Unavailable: " + ", ".join(sorted(errors)))
            return "\n".join(lines), False

        lines = [f'{total} matches for "{query}" across {len(names)} servers', ""]
        for name in names:
            found = matches.get(name)
            if found:
                lines.extend(
                    self._format_tools(
                        name, found, heading=f"{name}: {len(found)} matches"
                    )
                )
                lines.append("")
        if shown < total:
            lines.append(f"Showing {shown} of {total} matches; refine the query.")
        if errors:
            lines.append("Unavailable: " + ", ".join(sorted(errors)))
        lines.append('Inspect one: mcp_tools server="<name>" tool="<tool>"')
        return "\n".join(lines).rstrip(), False

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
            with self._session_use(name) as session:
                result = session.call_tool(tool, arguments)
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

    def _server_overview(self, refresh: bool = False) -> str:
        lines = [f"{len(self.specs)} upstream servers", ""]
        names = sorted(self.specs)
        tools_by_name, errors = self._prefetch(names, refresh=refresh)
        for name in sorted(self.specs):
            spec = self.specs[name]
            target = spec.url or f"{spec.command} {' '.join(spec.args)}".strip()
            if name in errors:
                count: Any = f"unavailable: {first_sentence(errors[name], 80)}"
            else:
                count = f"{len(tools_by_name.get(name, []))} tools"
            lines.append(f"- {name} [{spec.transport}] {count} <- {target}")
        lines.append("")
        lines.append('Inspect one: mcp_tools server="<name>"')
        return "\n".join(lines)

    def _reaper_loop(self, interval: float) -> None:
        while not self._reaper_stop.wait(interval):
            self._reap_idle()

    def _reap_idle(self) -> None:
        expired: List[Session] = []
        now = time.monotonic()
        with self._state_lock:
            if self._closed:
                return
            for name, spec in self.specs.items():
                timeout = spec.idle_timeout
                if timeout is None or self._active.get(name, 0):
                    continue
                last_used = self._last_used.get(name)
                if last_used is None or now - last_used < timeout:
                    continue
                session = self._sessions.pop(name, None)
                self._last_used.pop(name, None)
                if session is not None:
                    expired.append(session)
        for session in expired:
            session.close()

    def _unknown_server(self, name: Union[str, List[str]]) -> str:
        # Two very different causes produce this one sentence: a typo, or a name
        # that just left the config (Magpie rewrites library.json on refresh).
        # Without the second hint the caller retries the same spelling, which is
        # exactly what happened on 2026-11-13 (three retries, ~1 minute wasted).
        wanted = [name] if isinstance(name, str) else list(name)
        names = sorted(self.specs)
        listed = ", ".join(f"'{item}'" for item in wanted)
        hint = (
            f"Unknown server(s): {listed}. Available: {', '.join(names)}. "
            "If a server was listed a moment ago, the config changed under it: "
            "run mcp_tools with no arguments to re-list."
        )
        close = self._closest(wanted, names)
        if close:
            hint += f" Did you mean '{close}'?"
        return hint

    def _closest(self, wanted: List[str], names: List[str]) -> Optional[str]:
        """The nearest configured name, when one of the wanted is a near miss."""
        import difflib

        for item in wanted:
            matches = difflib.get_close_matches(item, names, n=1, cutoff=0.75)
            if matches:
                return matches[0]
        return None

    # -- MCP protocol ----------------------------------------------------
    def tools_spec(self) -> List[Dict[str, Any]]:
        names = ", ".join(sorted(self.specs))
        return [
            {
                "name": "mcp_tools",
                "description": (
                    "Discover MCP tools: list servers, list or search compact "
                    "signatures across servers, or show one tool's full "
                    "definition. Available servers: " + names
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "server": {
                            "type": "string",
                            "description": (
                                "One server name. Omit for an overview or "
                                "multi-server search."
                            ),
                        },
                        "servers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": (
                                "Limit a list or search to these server names."
                            ),
                        },
                        "tool": {
                            "type": "string",
                            "description": (
                                "With server, return this tool's full definition "
                                "and input schema."
                            ),
                        },
                        "query": {
                            "type": "string",
                            "description": (
                                "Case-insensitive terms matched against server "
                                "name, tool name, and tool description."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_SEARCH_LIMIT,
                            "default": DEFAULT_SEARCH_LIMIT,
                            "description": "Maximum number of search matches.",
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
        self._reaper_stop.set()
        if self._reaper is not None and self._reaper is not threading.current_thread():
            self._reaper.join(timeout=2)
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()


def serve(
    specs: Dict[str, ServerSpec],
    stream: Any = None,
    reload: Optional[Callable[[], Tuple[Dict[str, ServerSpec], str]]] = None,
) -> int:
    server = MetaServer(specs, reload=reload)
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
