"""Boot-time credential injection for MCP servers (MCP-3).

A module's ``manifest.mcp`` block declares MCP servers whose ``env`` (stdio) and
``headers`` (remote) maps reference secret env **NAMES** as ``${NAME}``
placeholders — never the values (the credential-safe invariant enforced on the
server by ``ModuleMcpValidator``).  Before an executor launches those servers,
the real values must be resolved from a linked ``service_connection`` via the
Superpos broker and substituted into the placeholders.

This module is the client-side half of that flow, mirroring the ``github_auth``
broker pattern: collect the NAMES a set of MCP servers declares, ask the broker
for their values, and inject them.  It intentionally does **not** launch MCP
child processes — the runtimes (Claude SDK / Codex config) own that.  Wire this
in right after ``collect_mcp_servers`` at boot.

Expansion discipline mirrors ``HostedAgentEnvResolver``: only exact ``${NAME}``
placeholders are substituted; an unresolved NAME is left literal so it fails
loudly at launch rather than silently becoming empty.  A literal value (which
the write-time validator forbids anyway) is never treated as a NAME and never
sent to the broker — so a ``KEY=value`` form can never round-trip through here.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# A single POSIX-subset placeholder: ${NAME}. Matches the server-side
# ModuleMcpValidator placeholder rule (an env/header value is "" or one ${VAR}).
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# The credential-bearing maps on an MCP server config.
_SECRET_FIELDS = ("env", "headers")


def extract_placeholder_names(servers: dict[str, Any]) -> set[str]:
    """Collect every ``${NAME}`` referenced across all servers' env / headers.

    ``servers`` is the merged ``serverName -> serverConfig`` map returned by
    ``collect_mcp_servers``.  Returns the set of distinct env NAMES that need
    resolving; empty when no server references any placeholder (an env-less MCP
    server, which must keep working untouched).
    """
    names: set[str] = set()
    for config in servers.values():
        if not isinstance(config, dict):
            continue
        for field in _SECRET_FIELDS:
            mapping = config.get(field)
            if not isinstance(mapping, dict):
                continue
            for value in mapping.values():
                if isinstance(value, str):
                    names.update(_PLACEHOLDER_RE.findall(value))
    return names


def inject_credentials(
    servers: dict[str, Any],
    values: dict[str, str],
) -> dict[str, Any]:
    """Return a copy of ``servers`` with ``${NAME}`` placeholders substituted.

    Only NAMES present in ``values`` are substituted; an unresolved placeholder
    is left literal (fail-loud downstream).  The input is not mutated.
    """
    resolved = copy.deepcopy(servers)
    for config in resolved.values():
        if not isinstance(config, dict):
            continue
        for field in _SECRET_FIELDS:
            mapping = config.get(field)
            if not isinstance(mapping, dict):
                continue
            for key, value in mapping.items():
                if isinstance(value, str) and "${" in value:
                    mapping[key] = _substitute(value, values)
    return resolved


def _substitute(value: str, values: dict[str, str]) -> str:
    """Replace each ``${NAME}`` in ``value`` with ``values[NAME]`` when known.

    Unknown NAMES are left as their literal ``${NAME}`` text.
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return values[name] if name in values else match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, value)


async def resolve_mcp_servers(
    servers: dict[str, Any],
    client: Any,
    *,
    service_connection_id: str | None,
) -> dict[str, Any]:
    """Resolve and inject MCP credentials for ``servers`` at boot.

    Collects the env NAMES the servers declare, resolves their values from
    ``service_connection_id`` via ``client.resolve_mcp_credentials``, and injects
    them.  Servers that reference no placeholder (or when no connection is linked)
    are returned unchanged — env-less and unlinked MCP servers keep working.

    Never sends a literal to the broker: only bare NAMES extracted from
    ``${NAME}`` placeholders are requested.
    """
    names = extract_placeholder_names(servers)
    if not names or not service_connection_id:
        return servers

    result = await client.resolve_mcp_credentials(
        service_connection_id, sorted(names),
    )
    values = result.get("credentials", {}) if isinstance(result, dict) else {}
    return inject_credentials(servers, values)
