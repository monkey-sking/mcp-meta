# mcp-meta

Expose dozens of MCP tools through **two**.

`mcp-meta` sits between your MCP client and any number of upstream MCP servers.
Instead of injecting every upstream tool schema into every request, it exposes
two tools:

| Tool | Purpose |
| --- | --- |
| `mcp_tools` | List servers, or one server's tools with compact signatures |
| `mcp_call` | Call one tool on one server |

Schemas are fetched lazily, only when the model asks for them.

## Why

Tool definitions are paid for on every request. A handful of MCP servers is
enough to add tens of thousands of characters of JSON schema before the model
has read a single line of your prompt. Measured on a 14-server setup:

```
14 servers / 104 tools  ->  142,746 characters per request
2 tools                 ->       892 characters per request   (-99.4%)
```

The model keeps full access to every upstream tool; it fetches the schema at
the moment it needs it.

## Install

```bash
pipx install git+https://github.com/monkey-sking/mcp-meta
# or
uv tool install git+https://github.com/monkey-sking/mcp-meta
```

Python 3.9+ and the standard library only. No dependencies.

## Configure

Create `~/.config/mcp-meta/servers.json`:

```json
{
  "servers": {
    "calc": {
      "url": "http://127.0.0.1:3425/mcp/calc"
    },
    "search": {
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${SEARCH_TOKEN}" },
      "timeout": 30
    },
    "memory": {
      "command": ["npx", "-y", "@modelcontextprotocol/server-memory"],
      "exclude": ["delete_entities"]
    }
  }
}
```

| Field | Meaning |
| --- | --- |
| `url` | HTTP endpoint of an upstream MCP server |
| `command` | Argv array for a stdio upstream, e.g. `["npx", "-y", "pkg"]` |
| `headers` | Extra HTTP headers; values expand `${VAR}` from the environment |
| `env` | Extra environment variables for a stdio upstream |
| `timeout` | Per-request timeout in seconds (default `60`) |
| `include` / `exclude` | Optional tool allowlist / denylist |

Config discovery order:

1. `--config PATH`
2. `MCP_META_CONFIG`
3. `~/.config/mcp-meta/servers.json`
4. `~/.config/magpie/library.json` (compatibility fallback)

Magpie's `library.json` works directly, so an existing Magpie Library can be
used as the upstream list without editing it.

## Use with an MCP client

Any client that speaks MCP over stdio can host it:

```json
{
  "mcpServers": {
    "meta": {
      "command": "mcp-meta",
      "args": ["serve"]
    }
  }
}
```

Or without installing:

```json
{
  "mcpServers": {
    "meta": {
      "command": "python3",
      "args": ["-m", "mcp_meta", "serve"],
      "cwd": "/path/to/mcp-meta"
    }
  }
}
```

## CLI

```bash
mcp-meta list                      # servers, transports, tool counts
mcp-meta call calc add '{"a": 2, "b": 3}'
mcp-meta serve --config ./servers.json
```

`list` and `call` are conveniences for debugging; the MCP client normally only
needs `serve`.

## How it behaves

- `mcp_tools` without a `server` prints a cheap overview; with a `server` it
  prints one line per tool, e.g. `search(query: string, [limit: integer])`,
  where brackets mark optional parameters.
- Tool lists are cached for 5 minutes per server. Pass `refresh: true` to
  bypass the cache.
- Upstream sessions are opened on first use and kept for the life of the
  process.
- HTTP upstreams are supported both as plain JSON and as SSE responses,
  including `Mcp-Session-Id` handling.
- Protocol revisions are negotiated downward (`2025-06-18` -> `2025-03-26` ->
  `2024-11-05`) so older servers still work.
- Tool errors are returned as tool output with `isError: true` rather than
  killing the session.

## Limits

- The front end is stdio only (what desktop MCP clients expect).
- `mcp_call` returns text content; non-text blocks are serialized as JSON.
- Resources, prompts, and sampling are not proxied.

## Before / after (this machine)

The project started as a 214-line personal script wrapping a Magpie Library.
This is the measured difference on the machine it came from, with 14 upstream
servers behind it:

| | Before | After |
| --- | --- | --- |
| Implementation | one-off script at `~/.dsh/bin/mcp-meta.py` | package installed with `uv tool` / `pipx` |
| Config source | hardcoded `~/.config/magpie/library.json` | own config, Magpie file as fallback |
| Upstream transports | HTTP only | HTTP + stdio |
| Registered as | `python3 ~/.dsh/bin/mcp-meta.py` | `mcp-meta serve` |
| Tool schema per request | 104 tools, 142,746 chars | 2 tools, 892 chars (-99.4%) |
| SSE handling | first `data:` line only | full multi-line SSE events |
| HTTP session | none | `Mcp-Session-Id` sent on every request |
| `tools/list` pagination | ignored | followed to the last page |
| Protocol negotiation | fixed `2024-11-05` | falls back across three revisions |
| Tool filtering | none | `include` / `exclude` per server |
| Tests | none | 15 (unit + HTTP/stdio integration + end-to-end) |

## Development

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
