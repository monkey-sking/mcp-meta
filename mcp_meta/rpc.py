"""JSON-RPC transports for upstream MCP servers (HTTP and stdio)."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from . import __version__
from .config import ServerSpec

# Newest first; the first rejection falls back to the previous revision.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

CLIENT_INFO = {"name": "mcp-meta", "version": __version__}


class UpstreamError(Exception):
    """A failure while talking to an upstream MCP server."""


def parse_sse(text: str) -> List[str]:
    """Return the ``data`` payload of every SSE event in ``text``."""
    events: List[str] = []
    data: List[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            if data:
                events.append("\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
    if data:
        events.append("\n".join(data))
    return events


class HttpTransport:
    def __init__(self, spec: ServerSpec) -> None:
        self.name = spec.name
        self.url = spec.url or ""
        self.headers = dict(spec.headers)
        self.timeout = spec.timeout
        self.session_id: Optional[str] = None

    def _post(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "mcp-meta/" + __version__,
        }
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                content_type = response.headers.get("Content-Type") or ""
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise UpstreamError(
                f"HTTP {exc.code} from {self.name}: {detail.strip()}"
            ) from None
        except urllib.error.URLError as exc:
            raise UpstreamError(
                f"cannot reach {self.name} at {self.url}: {exc.reason}"
            ) from None
        except OSError as exc:
            raise UpstreamError(f"cannot reach {self.name}: {exc}") from None

        if not raw.strip():
            return None
        if "text/event-stream" in content_type or raw.lstrip().startswith(
            ("event:", "data:", "id:")
        ):
            messages: List[Dict[str, Any]] = []
            for chunk in parse_sse(raw):
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    messages.append(parsed)
            for message in messages:
                if message.get("id") == payload.get("id") and (
                    "result" in message or "error" in message
                ):
                    return message
            for message in reversed(messages):
                if "result" in message or "error" in message:
                    return message
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            preview = raw.strip()[:200]
            raise UpstreamError(
                f"{self.name} returned non-JSON response: {preview}"
            ) from None
        return parsed if isinstance(parsed, dict) else None

    def notify(self, payload: Dict[str, Any]) -> None:
        self._post(payload)

    def request(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(payload)

    def close(self) -> None:
        if not self.session_id:
            return
        headers = {"Mcp-Session-Id": self.session_id}
        headers.update(self.headers)
        request = urllib.request.Request(
            self.url, headers=headers, method="DELETE"
        )
        try:
            urllib.request.urlopen(request, timeout=5).close()
        except Exception:  # noqa: BLE001 - session cleanup is best effort
            pass


class StdioTransport:
    def __init__(self, spec: ServerSpec) -> None:
        self.name = spec.name
        self.timeout = spec.timeout
        env = dict(os.environ)
        env.update(spec.env)
        command = [spec.command or ""] + list(spec.args)
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise UpstreamError(f"cannot start {self.name}: {exc}") from None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._write_lock = threading.Lock()

    def _read_stdout(self) -> None:
        stdout = self.process.stdout
        if stdout is None:
            self._lines.put(None)
            return
        for line in stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, payload: Dict[str, Any]) -> None:
        stdin = self.process.stdin
        if stdin is None or self.process.poll() is not None:
            raise UpstreamError(f"{self.name} exited (code {self.process.poll()})")
        with self._write_lock:
            stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stdin.flush()

    def request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._send(payload)
        import time as _time

        deadline = _time.monotonic() + self.timeout
        wanted = payload.get("id")
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise UpstreamError(f"{self.name} timed out after {self.timeout}s")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise UpstreamError(
                    f"{self.name} timed out after {self.timeout}s"
                ) from None
            if line is None:
                raise UpstreamError(
                    f"{self.name} closed its output (code {self.process.poll()})"
                )
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == wanted and (
                "result" in message or "error" in message
            ):
                return message

    def notify(self, payload: Dict[str, Any]) -> None:
        self._send(payload)

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=2)
        except Exception:  # noqa: BLE001 - shutdown is best effort
            try:
                self.process.kill()
            except Exception:  # noqa: BLE001
                pass


class Session:
    """An initialized MCP session against one upstream server."""

    def __init__(self, spec: ServerSpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.transport: Any
        if spec.transport == "http":
            self.transport = HttpTransport(spec)
        else:
            self.transport = StdioTransport(spec)
        self.server_info: Dict[str, Any] = {}
        self.protocol_version = ""
        self._next_id = 0
        self._started = False
        self._lock = threading.RLock()

    def _request_raw(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        message = self.transport.request(payload)
        if message is None:
            raise UpstreamError(f"{self.name} returned no response to {method}")
        if "error" in message:
            error = message["error"]
            text = error.get("message") if isinstance(error, dict) else str(error)
            raise UpstreamError(f"{self.name}: {text}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            last: Optional[UpstreamError] = None
            for version in PROTOCOL_VERSIONS:
                try:
                    result = self._request_raw(
                        "initialize",
                        {
                            "protocolVersion": version,
                            "capabilities": {},
                            "clientInfo": CLIENT_INFO,
                        },
                    )
                except UpstreamError as exc:
                    last = exc
                    continue
                self.server_info = result.get("serverInfo") or {}
                self.protocol_version = result.get("protocolVersion") or version
                self.transport.notify(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
                self._started = True
                return
            raise last or UpstreamError(f"{self.name}: initialize failed")

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self.start()
            return self._request_raw(method, params or {})

    def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = self.request("tools/list", params)
            found = result.get("tools")
            if isinstance(found, list):
                tools.extend(t for t in found if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self.request(
            "tools/call", {"name": tool, "arguments": arguments}
        )

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001 - shutdown is best effort
            pass
