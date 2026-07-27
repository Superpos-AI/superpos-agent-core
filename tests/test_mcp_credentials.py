"""Tests for boot-time MCP credential injection (mcp_credentials, MCP-3).

Network is never touched: the broker client is a fake that returns a fixed
``credentials`` map, mirroring the ``github_auth`` test style.
"""

from __future__ import annotations

import pytest

from superpos_agent_core import mcp_credentials as mc


class _FakeBrokerClient:
    """Stand-in for SuperposClient.resolve_mcp_credentials."""

    def __init__(self, credentials: dict[str, str]):
        self._credentials = credentials
        self.calls: list[tuple[str, list[str]]] = []

    async def resolve_mcp_credentials(self, service_connection_id, keys):
        self.calls.append((service_connection_id, list(keys)))
        return {
            "service_connection_id": service_connection_id,
            "credentials": {k: v for k, v in self._credentials.items() if k in keys},
            "missing": [k for k in keys if k not in self._credentials],
            "expires_at": None,
        }


# ── extract_placeholder_names ─────────────────────────────────────────


def test_extract_collects_env_and_header_names():
    servers = {
        "sentry": {"url": "https://mcp.sentry.dev", "headers": {"Authorization": "${SENTRY_TOKEN}"}},
        "local": {"command": "npx", "args": ["-y", "srv"], "env": {"API_KEY": "${API_KEY}"}},
    }
    assert mc.extract_placeholder_names(servers) == {"SENTRY_TOKEN", "API_KEY"}


def test_extract_ignores_env_less_servers():
    servers = {"plain": {"command": "npx", "args": ["-y", "plain-server"]}}
    assert mc.extract_placeholder_names(servers) == set()


# ── inject_credentials (pure) ─────────────────────────────────────────


def test_inject_into_headers_for_remote_server():
    # A remote MCP + headers-with-NAMES resolves correctly.
    servers = {"sentry": {"url": "https://mcp.sentry.dev", "headers": {"Authorization": "${SENTRY_TOKEN}"}}}
    resolved = mc.inject_credentials(servers, {"SENTRY_TOKEN": "sk-real-token"})
    assert resolved["sentry"]["headers"]["Authorization"] == "sk-real-token"


def test_inject_into_env_for_stdio_server():
    # A stdio MCP + env-NAME is resolved.
    servers = {"local": {"command": "npx", "args": ["-y", "srv"], "env": {"API_KEY": "${API_KEY}"}}}
    resolved = mc.inject_credentials(servers, {"API_KEY": "the-value"})
    assert resolved["local"]["env"]["API_KEY"] == "the-value"


def test_inject_leaves_unresolved_placeholder_literal():
    servers = {"local": {"command": "npx", "env": {"MISSING": "${MISSING}"}}}
    resolved = mc.inject_credentials(servers, {"OTHER": "x"})
    # Unresolved -> left literal, so it fails loudly at launch, not silently empty.
    assert resolved["local"]["env"]["MISSING"] == "${MISSING}"


def test_inject_does_not_mutate_input():
    servers = {"local": {"command": "npx", "env": {"API_KEY": "${API_KEY}"}}}
    mc.inject_credentials(servers, {"API_KEY": "secret"})
    assert servers["local"]["env"]["API_KEY"] == "${API_KEY}"


# ── resolve_mcp_servers (orchestration) ───────────────────────────────


async def test_resolve_injects_from_broker():
    servers = {
        "sentry": {"url": "https://mcp.sentry.dev", "headers": {"Authorization": "${SENTRY_TOKEN}"}},
        "local": {"command": "npx", "env": {"API_KEY": "${API_KEY}"}},
    }
    client = _FakeBrokerClient({"SENTRY_TOKEN": "tok", "API_KEY": "key"})

    resolved = await mc.resolve_mcp_servers(servers, client, service_connection_id="conn-1")

    assert resolved["sentry"]["headers"]["Authorization"] == "tok"
    assert resolved["local"]["env"]["API_KEY"] == "key"
    # The broker was asked for exactly the declared NAMES (sorted, deduped).
    assert client.calls == [("conn-1", ["API_KEY", "SENTRY_TOKEN"])]


async def test_resolve_skips_broker_for_env_less_servers():
    # Backward compat: an MCP block WITHOUT env NAMES still loads, no broker call.
    servers = {"plain": {"command": "npx", "args": ["-y", "plain-server"]}}
    client = _FakeBrokerClient({})

    resolved = await mc.resolve_mcp_servers(servers, client, service_connection_id="conn-1")

    assert resolved == servers
    assert client.calls == []


async def test_resolve_skips_broker_without_connection():
    servers = {"local": {"command": "npx", "env": {"API_KEY": "${API_KEY}"}}}
    client = _FakeBrokerClient({"API_KEY": "key"})

    resolved = await mc.resolve_mcp_servers(servers, client, service_connection_id=None)

    # No linked connection -> placeholder left literal, no broker call.
    assert resolved["local"]["env"]["API_KEY"] == "${API_KEY}"
    assert client.calls == []


async def test_resolve_only_sends_bare_names_never_literals():
    # A literal value (which the write-time validator forbids) is never treated
    # as a NAME and never reaches the broker — a KEY=value form cannot leak here.
    servers = {"local": {"command": "npx", "env": {"API_KEY": "literal=value"}}}
    client = _FakeBrokerClient({"API_KEY": "key"})

    resolved = await mc.resolve_mcp_servers(servers, client, service_connection_id="conn-1")

    assert client.calls == []
    assert resolved["local"]["env"]["API_KEY"] == "literal=value"
