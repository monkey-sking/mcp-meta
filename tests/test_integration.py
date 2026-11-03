import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp_meta.config import parse_config
from mcp_meta.rpc import Session, parse_sse
from mcp_meta.server import MetaServer, format_signature

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HTTP_TOOLS = [
    {
        "name": "search",
        "description": "Search the web. Returns ranked results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    }
]


class FakeHttpMcp(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_session_header = None

    def log_message(self, *args):  # silence test noise
        pass

    def _send_json(self, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, messages):
        chunks = []
        for message in messages:
            chunks.append("event: message\ndata: " + json.dumps(message) + "\n\n")
        body = "".join(chunks).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        FakeHttpMcp.seen_session_header = self.headers.get("Mcp-Session-Id")
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            registry = self.server.registry
            registry["session"] = self.headers.get("Mcp-Session-Id")
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": request["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-http", "version": "1.2.3"},
                    },
                },
                headers={"Mcp-Session-Id": "sess-123"},
            )
            return
        registry = self.server.registry
        registry["later_session"] = self.headers.get("Mcp-Session-Id")
        if method == "tools/list":
            self._send_sse(
                [
                    {"jsonrpc": "2.0", "method": "notifications/message", "params": {}},
                    {"jsonrpc": "2.0", "id": request_id, "result": {"tools": HTTP_TOOLS}},
                ]
            )
        elif method == "tools/call":
            params = request.get("params") or {}
            text = "results for " + str(params.get("arguments", {}).get("query"))
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    },
                }
            )
        else:
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "nope"},
                }
            )


class HttpSessionTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHttpMcp)
        self.server.registry = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.specs = parse_config(
            {"servers": {"web": {"url": f"http://127.0.0.1:{port}/mcp"}}}
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_session_reuses_id_and_parses_sse(self):
        session = Session(self.specs["web"])
        try:
            tools = session.list_tools()
            self.assertEqual([t["name"] for t in tools], ["search"])
            self.assertEqual(self.server.registry["session"], None)
            self.assertEqual(self.server.registry["later_session"], "sess-123")
            result = session.call_tool("search", {"query": "mcp"})
            self.assertEqual(result["content"][0]["text"], "results for mcp")
        finally:
            session.close()
        self.assertEqual(FakeHttpMcp.seen_session_header, "sess-123")

    def test_meta_server_overview_and_call(self):
        meta = MetaServer(self.specs)
        try:
            overview, is_error = meta.tool_mcp_tools({})
            self.assertFalse(is_error)
            self.assertIn("web [http] 1 tools", overview)

            listing, is_error = meta.tool_mcp_tools({"server": "web"})
            self.assertFalse(is_error)
            self.assertIn("search(query: string, [limit: integer])", listing)

            text, is_error = meta.tool_mcp_call(
                {"server": "web", "tool": "search", "arguments": {"query": "x"}}
            )
            self.assertFalse(is_error)
            self.assertEqual(text, "results for x")
        finally:
            meta.close()

    def test_filtered_tool_is_rejected(self):
        specs = parse_config(
            {
                "servers": {
                    "web": {
                        "url": self.specs["web"].url,
                        "exclude": ["search"],
                    }
                }
            }
        )
        meta = MetaServer(specs)
        try:
            _, is_error = meta.tool_mcp_call(
                {"server": "web", "tool": "search", "arguments": {}}
            )
            self.assertTrue(is_error)
        finally:
            meta.close()


class StdioSessionTests(unittest.TestCase):
    def test_protocol_fallback_and_pagination(self):
        fixture = os.path.join(ROOT, "tests", "fixtures", "fake_stdio_server.py")
        specs = parse_config(
            {"servers": {"fake": {"command": [sys.executable, fixture]}}}
        )
        session = Session(specs["fake"])
        try:
            self.assertEqual(session.protocol_version, "")
            tools = session.list_tools()
            self.assertEqual([t["name"] for t in tools], ["echo", "add"])
            self.assertEqual(session.protocol_version, "2024-11-05")
            self.assertEqual(session.server_info["name"], "fake")
            result = session.call_tool("add", {"a": 2, "b": 3})
            self.assertEqual(result["content"][0]["text"], "5")
        finally:
            session.close()


class ServeEndToEndTests(unittest.TestCase):
    def test_stdio_front_end(self):
        fixture = os.path.join(ROOT, "tests", "fixtures", "fake_stdio_server.py")
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "servers.json")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump(
                    {"servers": {"fake": {"command": [sys.executable, fixture]}}},
                    handle,
                )
            proc = subprocess.Popen(
                [sys.executable, "-m", "mcp_meta", "serve", "--config", config],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                def send(payload):
                    proc.stdin.write(json.dumps(payload) + "\n")
                    proc.stdin.flush()
                    return json.loads(proc.stdout.readline())

                init = send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"},
                    }
                )
                self.assertEqual(init["result"]["serverInfo"]["name"], "mcp-meta")

                listed = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                names = [t["name"] for t in listed["result"]["tools"]]
                self.assertEqual(names, ["mcp_tools", "mcp_call"])

                called = send(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "mcp_call",
                            "arguments": {
                                "server": "fake",
                                "tool": "echo",
                                "arguments": {"text": "hi"},
                            },
                        },
                    }
                )
                self.assertEqual(called["result"]["content"][0]["text"], "echo:hi")
            finally:
                proc.stdin.close()
                proc.wait(timeout=10)
                proc.stdout.close()
                proc.stderr.close()


class FormatTests(unittest.TestCase):
    def test_signature_marks_optional_parameters(self):
        signature = format_signature(HTTP_TOOLS[0])
        self.assertEqual(signature, "search(query: string, [limit: integer])")

    def test_sse_multiline_data(self):
        events = parse_sse("event: x\ndata: {\"a\":\ndata: 1}\n\n")
        self.assertEqual(events, ['{"a":\n1}'])


if __name__ == "__main__":
    unittest.main()
