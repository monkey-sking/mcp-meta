import contextlib
import io
import json
import os
import tempfile
import unittest

from mcp_meta.__main__ import main
from mcp_meta.config import (
    ConfigError,
    load_config,
    normalize_config,
    parse_config,
)


class ParseConfigTests(unittest.TestCase):
    def test_example_config_parses(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(root, "config.example.json"), encoding="utf-8"
        ) as handle:
            specs = parse_config(json.load(handle))
        self.assertEqual(
            specs["localserver"].cwd, os.path.expanduser("~/dev/localserver")
        )
        self.assertEqual(specs["localserver"].idle_timeout, 900.0)

    def test_url_string_mapping(self):
        specs = parse_config({"servers": {"calc": "http://localhost:1/mcp"}})
        self.assertEqual(specs["calc"].transport, "http")
        self.assertEqual(specs["calc"].url, "http://localhost:1/mcp")

    def test_stdio_command_and_filters(self):
        specs = parse_config(
            {
                "servers": {
                    "memory": {
                        "command": ["npx", "-y", "server-memory"],
                        "env": {"TOKEN": "x"},
                        "exclude": ["delete_everything"],
                    }
                }
            }
        )
        memory = specs["memory"]
        self.assertEqual(memory.transport, "stdio")
        self.assertEqual(memory.command, "npx")
        self.assertEqual(memory.args, ["-y", "server-memory"])
        self.assertTrue(memory.allow_tool("read_graph"))
        self.assertFalse(memory.allow_tool("delete_everything"))

    def test_client_config_shape_cwd_idle_timeout_and_disabled(self):
        specs = parse_config(
            {
                "mcpServers": {
                    "files": {
                        "command": "npx",
                        "args": ["-y", "server-files"],
                        "cwd": "~/project",
                        "idle_timeout": 15,
                    },
                    "disabled": {
                        "command": "npx",
                        "args": ["-y", "server-disabled"],
                        "disabled": True,
                    },
                }
            }
        )
        self.assertEqual(sorted(specs), ["files"])
        self.assertEqual(specs["files"].command, "npx")
        self.assertEqual(specs["files"].args, ["-y", "server-files"])
        self.assertEqual(specs["files"].cwd, os.path.expanduser("~/project"))
        self.assertEqual(specs["files"].idle_timeout, 15)

    def test_magpie_library_shape(self):
        specs = parse_config(
            {
                "version": 1,
                "mcp": [
                    {"name": "calc", "url": "http://127.0.0.1:3425/mcp/calc"},
                    {"name": "exa", "url": "http://127.0.0.1:3425/mcp/exa"},
                ],
            }
        )
        self.assertEqual(sorted(specs), ["calc", "exa"])

    def test_missing_target_is_an_error(self):
        with self.assertRaises(ConfigError):
            parse_config({"servers": {"broken": {}}})

    def test_empty_config_is_an_error(self):
        with self.assertRaises(ConfigError):
            parse_config({"servers": {}})

    def test_env_references_are_expanded(self):
        os.environ["MCP_META_TEST_TOKEN"] = "secret-value"
        try:
            specs = parse_config(
                {
                    "servers": {
                        "web": {
                            "url": "https://example.com/mcp",
                            "headers": {
                                "Authorization": "Bearer ${MCP_META_TEST_TOKEN}"
                            },
                        }
                    }
                }
            )
        finally:
            del os.environ["MCP_META_TEST_TOKEN"]
        self.assertEqual(
            specs["web"].headers["Authorization"], "Bearer secret-value"
        )


class ImportConfigTests(unittest.TestCase):
    def test_normalize_keeps_env_references_and_drops_disabled(self):
        doc = {
            "mcpServers": {
                "web": {
                    "command": "npx",
                    "args": ["-y", "pkg"],
                    "headers": {"Authorization": "Bearer ${TOKEN}"},
                },
                "off": {"url": "http://example.com/mcp", "disabled": True},
            }
        }
        normalized = normalize_config(doc)
        self.assertEqual(list(normalized["servers"]), ["web"])
        self.assertEqual(
            normalized["servers"]["web"]["headers"]["Authorization"],
            "Bearer ${TOKEN}",
        )
        self.assertEqual(parse_config(normalized)["web"].args, ["-y", "pkg"])

    def test_import_command_writes_converted_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "claude_desktop_config.json")
            with open(source, "w", encoding="utf-8") as handle:
                json.dump(
                    {"mcpServers": {"calc": {"url": "http://localhost:9/mcp"}}},
                    handle,
                )
            target = os.path.join(tmp, "nested", "servers.json")
            quiet = contextlib.redirect_stdout(io.StringIO())
            with quiet, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["import", source, "--output", target]), 0)
                specs, _ = load_config(target)
                self.assertIn("calc", specs)
                self.assertEqual(main(["import", source, "--output", target]), 2)
                self.assertEqual(
                    main(["import", source, "--output", target, "--force"]), 0
                )


class LoadConfigTests(unittest.TestCase):
    def test_explicit_path_and_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "servers.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"servers": {"calc": "http://localhost:9/mcp"}}, handle)
            specs, source = load_config(path)
            self.assertEqual(source, path)
            self.assertIn("calc", specs)

            os.environ["MCP_META_CONFIG"] = path
            try:
                _, env_source = load_config()
            finally:
                del os.environ["MCP_META_CONFIG"]
            self.assertEqual(env_source, path)

    def test_invalid_json_reports_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{nope")
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn(path, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
