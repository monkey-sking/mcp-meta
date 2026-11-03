#!/usr/bin/env python3
"""Minimal stdio MCP server used by the tests.

Accepts only the 2024-11-05 protocol revision so the client's version
fallback path is exercised. ``tools/list`` is paginated on purpose.
"""

import json
import sys


def reply(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": message}}


TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the text you pass in.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two integers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
]


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            version = (request.get("params") or {}).get("protocolVersion")
            if version != "2024-11-05":
                response = error(request_id, "unsupported protocol " + str(version))
            else:
                response = reply(
                    request_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "0.0.1"},
                    },
                )
        elif method == "tools/list":
            cursor = (request.get("params") or {}).get("cursor")
            if cursor is None:
                response = reply(
                    request_id, {"tools": TOOLS[:1], "nextCursor": "page-2"}
                )
            else:
                response = reply(request_id, {"tools": TOOLS[1:]})
        elif method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                text = "echo:" + str(args.get("text", ""))
            elif name == "add":
                text = str(int(args.get("a", 0)) + int(args.get("b", 0)))
            else:
                sys.stdout.write(json.dumps(error(request_id, "no such tool")) + "\n")
                sys.stdout.flush()
                continue
            response = reply(
                request_id,
                {"content": [{"type": "text", "text": text}], "isError": False},
            )
        else:
            response = error(request_id, "unknown method")
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
