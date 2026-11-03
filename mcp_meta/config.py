"""Server configuration loading.

The config is a JSON document with a ``servers`` object:

    {"servers": {"calc": {"url": "http://127.0.0.1:3425/mcp/calc"}}}

Two compatibility shapes are also accepted: a bare ``{name: url}`` mapping and
Magpie's ``library.json`` (``{"mcp": [{"name": ..., "url": ...}]}``).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

USER_CONFIG_PATHS = (
    "~/.config/mcp-meta/servers.json",
    "~/.config/mcp-meta/config.json",
)

MAGPIE_LIBRARY = "~/.config/magpie/library.json"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised when the server configuration cannot be read or parsed."""


@dataclass
class ServerSpec:
    name: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    include: Optional[List[str]] = None
    exclude: List[str] = field(default_factory=list)

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def allow_tool(self, tool: str) -> bool:
        if self.include is not None and tool not in self.include:
            return False
        return tool not in self.exclude


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _str_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): _expand(str(v)) for k, v in value.items()}


def _expand(value: str) -> str:
    """Replace ``${VAR}`` with the environment value (empty when unset)."""
    return _ENV_REF.sub(lambda match: os.environ.get(match.group(1), ""), value)


def _parse_spec(name: str, raw: Any) -> ServerSpec:
    if isinstance(raw, str):
        return ServerSpec(name=name, url=raw)
    if not isinstance(raw, dict):
        raise ConfigError(f"server '{name}' must be a URL string or an object")

    url = raw.get("url")
    command = raw.get("command")
    if isinstance(command, list):
        parts = [str(p) for p in command]
        command = parts[0] if parts else None
        args = parts[1:]
    else:
        args = [str(a) for a in _as_list(raw.get("args"))]

    if not url and not command:
        raise ConfigError(f"server '{name}' needs either 'url' or 'command'")

    timeout = raw.get("timeout", 60)
    include = raw.get("include")
    return ServerSpec(
        name=str(raw.get("name") or name),
        url=_expand(str(url)) if url else None,
        command=str(command) if command else None,
        args=args,
        env=_str_map(raw.get("env")),
        headers=_str_map(raw.get("headers")),
        timeout=float(timeout),
        include=[str(t) for t in include] if isinstance(include, list) else None,
        exclude=[str(t) for t in _as_list(raw.get("exclude"))],
    )


def parse_config(doc: Any) -> Dict[str, ServerSpec]:
    if not isinstance(doc, dict):
        raise ConfigError("config root must be a JSON object")

    if "servers" in doc:
        entries = doc["servers"]
    elif isinstance(doc.get("mcp"), list):
        entries = doc["mcp"]
    elif isinstance(doc.get("mcp"), dict):
        entries = doc["mcp"]
    else:
        entries = doc

    specs: Dict[str, ServerSpec] = {}
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict) or not item.get("name"):
                raise ConfigError("every server entry needs a 'name'")
            spec = _parse_spec(str(item["name"]), item)
            specs[spec.name] = spec
    elif isinstance(entries, dict):
        for name, item in entries.items():
            if name.startswith("$"):
                continue
            spec = _parse_spec(str(name), item)
            specs[spec.name] = spec
    else:
        raise ConfigError("'servers' must be an object or a list")

    if not specs:
        raise ConfigError("no usable MCP servers found in config")
    return specs


def resolve_config_path(path: Optional[str] = None) -> Optional[str]:
    if path:
        return os.path.expanduser(path)
    env_path = os.environ.get("MCP_META_CONFIG")
    if env_path:
        return os.path.expanduser(env_path)
    for candidate in USER_CONFIG_PATHS:
        expanded = os.path.expanduser(candidate)
        if os.path.exists(expanded):
            return expanded
    magpie = os.path.expanduser(MAGPIE_LIBRARY)
    if os.path.exists(magpie):
        return magpie
    return None


def load_config(path: Optional[str] = None) -> Tuple[Dict[str, ServerSpec], str]:
    resolved = resolve_config_path(path)
    if not resolved:
        raise ConfigError(
            "no config found; create ~/.config/mcp-meta/servers.json "
            "or pass --config"
        )
    try:
        with open(resolved, encoding="utf-8") as handle:
            doc = json.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read {resolved}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {resolved}: {exc}") from None
    return parse_config(doc), resolved
