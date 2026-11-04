"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__
from .config import ConfigError, load_config, normalize_config, parse_config
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

    importer = sub.add_parser(
        "import",
        help="Convert a client config (Claude Desktop, Cursor, Magpie) "
        "into mcp-meta's servers.json",
    )
    importer.add_argument("path", help="Client config file to read")
    importer.add_argument("--output", help="Where to write the converted file")
    importer.add_argument(
        "--stdout", action="store_true", help="Print instead of writing a file"
    )
    importer.add_argument(
        "--force", action="store_true", help="Overwrite an existing output file"
    )
    return parser


def run_import(args: argparse.Namespace) -> int:
    path = os.path.expanduser(args.path)
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except OSError as exc:
        sys.stderr.write(f"mcp-meta: cannot read {path}: {exc}\n")
        return 2
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"mcp-meta: invalid JSON in {path}: {exc}\n")
        return 2

    try:
        parse_config(doc)  # validate the source before rewriting it
        normalized = normalize_config(doc)
    except ConfigError as exc:
        sys.stderr.write(f"mcp-meta: {exc}\n")
        return 2

    text = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    if args.stdout:
        sys.stdout.write(text)
        return 0

    target = os.path.expanduser(
        args.output or "~/.config/mcp-meta/servers.json"
    )
    if os.path.exists(target) and not args.force:
        sys.stderr.write(
            f"mcp-meta: {target} already exists; pass --force to overwrite\n"
        )
        return 2
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {len(normalized['servers'])} servers to {target}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = getattr(args, "config", None) or args.config_global

    if args.command == "import":
        return run_import(args)

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
