"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import __version__
from .config import ConfigError, load_config
from .rpc import UpstreamError
from .server import MetaServer, serve


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        help="Path to servers.json (default: ~/.config/mcp-meta/servers.json)",
    )

    parser = argparse.ArgumentParser(
        prog="mcp-meta",
        description="Expose many MCP servers through two lazy tools.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", dest="config_global", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "serve", parents=[common], help="Run the MCP server over stdio (default)"
    )
    sub.add_parser(
        "list", parents=[common], help="Print the configured servers and tool counts"
    )

    call = sub.add_parser(
        "call", parents=[common], help="Call one upstream tool from the CLI"
    )
    call.add_argument("server")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = getattr(args, "config", None) or args.config_global

    try:
        specs, source = load_config(config_path)
    except ConfigError as exc:
        sys.stderr.write(f"mcp-meta: {exc}\n")
        return 2

    if args.command == "list":
        print(f"config: {source}")
        server = MetaServer(specs)
        try:
            print(server.tool_mcp_tools({})[0])
        finally:
            server.close()
        return 0

    if args.command == "call":
        try:
            arguments = json.loads(args.arguments)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"mcp-meta: invalid JSON arguments: {exc}\n")
            return 2
        server = MetaServer(specs)
        try:
            text, is_error = server.tool_mcp_call(
                {"server": args.server, "tool": args.tool, "arguments": arguments}
            )
        finally:
            server.close()
        print(text)
        return 1 if is_error else 0

    sys.stderr.write(f"mcp-meta: serving {len(specs)} servers from {source}\n")
    return serve(specs)


if __name__ == "__main__":
    raise SystemExit(main())
