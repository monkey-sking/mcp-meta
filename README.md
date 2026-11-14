# mcp-meta

<!-- badges:start -->
[![Stars](https://img.shields.io/github/stars/monkey-sking/mcp-meta?style=flat-square)](https://github.com/monkey-sking/mcp-meta/stargazers) [![Forks](https://img.shields.io/github/forks/monkey-sking/mcp-meta?style=flat-square)](https://github.com/monkey-sking/mcp-meta/network/members) [![Last commit](https://img.shields.io/github/last-commit/monkey-sking/mcp-meta?style=flat-square)](https://github.com/monkey-sking/mcp-meta/commits/main) [![License](https://img.shields.io/github/license/monkey-sking/mcp-meta?style=flat-square)](https://github.com/monkey-sking/mcp-meta/blob/main/LICENSE) [![Python](https://img.shields.io/github/languages/top/monkey-sking/mcp-meta?style=flat-square)](https://github.com/monkey-sking/mcp-meta/search?l=Python) [![Tests](https://github.com/monkey-sking/mcp-meta/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/monkey-sking/mcp-meta/actions/workflows/tests.yml) [![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)
<!-- badges:end -->

Expose dozens of MCP tools through **two**.

Part of the meta-tool family: [skill-meta](https://github.com/monkey-sking/skill-meta) applies the same lazy-facade idea to **Agent Skills** (3 tools for N skill catalogs across DSH/Claude/Codex/Magpie).

`mcp-meta` sits between your MCP client and any number of upstream MCP servers.
Instead of injecting every upstream tool schema into every request, it exposes
two tools:

| Tool | Purpose |
| --- | --- |
| `mcp_tools` | Overview, compact signatures, cross-server search, or one full schema |
| `mcp_call` | Call one tool on one server |

Schemas are fetched lazily, and only as deep as the model asks for: a server
overview, then compact one-line signatures, then a single tool's complete JSON
schema when the arguments need to be exact.

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
| `command` | Argv array for a stdio upstream, e.g. `["npx", "-y", "pkg"]`; a `command` string plus an `args` array also works |
| `cwd` | Working directory for a stdio upstream |
| `headers` | Extra HTTP headers; values expand `${VAR}` from the environment |
| `env` | Extra environment variables for a stdio upstream |
| `timeout` | Per-request timeout in seconds (default `60`) |
| `idle_timeout` | Seconds of inactivity before a stdio upstream is stopped (default: keep it for the life of the process) |
| `include` / `exclude` | Optional tool allowlist / denylist |
| `disabled` | `true` skips the entry |

Config discovery order:

1. `--config PATH`
2. `MCP_META_CONFIG`
3. `~/.config/mcp-meta/servers.json`
4. `~/.config/magpie/library.json` (compatibility fallback)

Magpie's `library.json` works directly, so an existing Magpie Library can be
used as the upstream list without editing it.

Claude Desktop and Cursor configs work too: the `mcpServers` key and the
`command` + `args` split are read as-is, so a client config can be used
directly as `--config`, or converted into mcp-meta's own shape:

```bash
mcp-meta import ~/Library/Application\ Support/Claude/claude_desktop_config.json
# wrote 6 servers to ~/.config/mcp-meta/servers.json
```

`import` copies entries verbatim, so `${VAR}` references are not expanded into
the file, and it will not overwrite an existing `servers.json` without
`--force`. Use `--stdout` to inspect the result first.

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
mcp-meta import client-config.json # convert a Claude/Cursor/Magpie config
mcp-meta call calc add '{"a": 2, "b": 3}'
mcp-meta serve --config ./servers.json
```

`list` and `call` are conveniences for debugging; the MCP client normally only
needs `serve`.

## How it behaves

- `mcp_tools` without arguments prints a cheap overview, one line per server.
- `mcp_tools server="x"` prints one line per tool, e.g.
  `search(query: string, [limit: integer])`, where brackets mark optional
  parameters.
- `mcp_tools server="x" tool="y"` returns that tool's complete JSON definition
  — nested objects, `enum` values, `required` lists — so arguments never have
  to be guessed from a compact signature.
- `mcp_tools servers=["a", "b"]` lists several servers in one call.
- `mcp_tools query="file"` searches server names, tool names and descriptions
  across every server (case-insensitive, all words must match). `limit` caps
  the output at 50 matches by default, 200 at most.
- Queries that span servers fetch them in parallel, up to 8 at a time; a
  single server's `tools/list` stays serialized.
- Tool lists are cached for 5 minutes per server. Pass `refresh: true` to
  bypass the cache.
- The server list itself is read once, at startup: Magpie rewrites
  `library.json` when connectors change, so a name can vanish from under a
  running process. An unknown server therefore answers with the current
  names, a near-miss suggestion, and the way back — `mcp_tools` with no
  arguments re-lists and restarts from the file as it is now.
- Upstream sessions are opened on first use and kept for the life of the
  process. With `idle_timeout` set, an idle stdio upstream is stopped and
  restarted transparently on the next call.
- HTTP upstreams are supported both as plain JSON and as SSE responses,
  including `Mcp-Session-Id` handling.
- Protocol revisions are negotiated downward (`2025-06-18` -> `2025-03-26` ->
  `2024-11-05`) so older servers still work.
- Tool errors are returned as tool output with `isError: true` rather than
  killing the session.

## Limits

- The front end is stdio only (what desktop MCP clients expect).
- `mcp_call` returns text content; non-text blocks are serialized as JSON.
- `query` matches server names, tool names and descriptions; it does not search
  inside parameter schemas.
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
| Config formats | one hardcoded shape | Claude/Cursor `mcpServers`, Magpie, bare mappings |
| Tool detail | signature only | `server` + `tool` returns the full JSON schema |
| Long-running stdio upstreams | never released | optional `idle_timeout` reaping |
| Tests | none | 24 (unit + HTTP/stdio integration + end-to-end) |

## Development

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
