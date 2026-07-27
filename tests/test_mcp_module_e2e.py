"""End-to-end agent-side MCP module test (MCP-4).

Stitches the FULL agent-side chain for a registry-resolved MCP module and
asserts the remote-HTTP server survives every hop:

    registry-resolved payload (manifest.mcp)
        → registry_overlay.overlay_modules  (writes module.yaml on disk)
        → module_loader.discover_modules     (reads module.yaml back)
        → module_loader.collect_mcp_servers  (merges into one dict)

The assertions are on the CONCRETE server (name + url), so the test fails
the moment ``mcp`` is dropped anywhere in the chain — it is non-vacuous by
construction.

It also proves the NAMES-only invariant end-to-end: a header placeholder
``${EXAMPLE_MCP_TOKEN}`` survives *verbatim*.  Neither the overlay nor the
collector ever resolves placeholder values — that is the credential
broker's job at boot (server-side POST /api/v1/mcp/credentials).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from superpos_agent_core.module_loader import (
    collect_mcp_servers,
    discover_modules,
)
from superpos_agent_core.registry_overlay import overlay_modules

# ── Canonical MCP-4 example module ───────────────────────────────────
#
# Shape mirrors the registry-resolved module entries in
# tests/test_registry_overlay.py (slug / name / manifest / files / skill).
# ``manifest.mcp`` is passed through to the top-level ``mcp:`` key of the
# written module.yaml verbatim by the overlay.

EXAMPLE_REMOTE_URL = "https://mcp.example.com/sse"


def _example_remote_mcp_module() -> dict:
    """The canonical ``example-remote-mcp`` registry-resolved module."""
    return {
        "slug": "example-remote-mcp",
        "name": "example-remote-mcp",
        "manifest": {
            "name": "example-remote-mcp",
            "description": "Example remote-HTTP MCP module (MCP-4 reference).",
            "env_keys": [],
            "mcp": {
                "example-remote": {"url": EXAMPLE_REMOTE_URL},
            },
        },
        "files": [],
        "skill": (
            "# example-remote-mcp\n\n"
            "Reference MCP module for MCP-4.  Exposes the `example-remote`\n"
            "remote-HTTP MCP server at "
            f"{EXAMPLE_REMOTE_URL}.\n"
        ),
    }


def _example_remote_mcp_module_with_header() -> dict:
    """Same module but carrying a NAMES-only ``${VAR}`` header placeholder.

    Proves the overlay/collector never resolve placeholder values — the
    literal ``${EXAMPLE_MCP_TOKEN}`` must survive verbatim to boot, where
    the server-side broker resolves it.
    """
    mod = _example_remote_mcp_module()
    mod["slug"] = "example-remote-mcp-auth"
    mod["name"] = "example-remote-mcp-auth"
    mod["manifest"]["name"] = "example-remote-mcp-auth"
    mod["manifest"]["mcp"] = {
        "example-remote": {
            "url": EXAMPLE_REMOTE_URL,
            "headers": {"Authorization": "${EXAMPLE_MCP_TOKEN}"},
        },
    }
    return mod


# ── The end-to-end chain ─────────────────────────────────────────────


def test_remote_mcp_survives_overlay_to_collect(tmp_path: Path):
    """Full chain: overlay writes module.yaml → discover → collect.

    Asserts the concrete ``example-remote`` server with its exact url is
    present in the merged MCP dict.  Fails if ``mcp`` is dropped at any hop.
    """
    modules_dir = tmp_path / "modules"

    # 1) Run the REAL overlay install so a real module.yaml lands on disk.
    result = overlay_modules([_example_remote_mcp_module()], str(modules_dir))
    assert "example-remote-mcp" in result.installed
    assert "example-remote-mcp" not in result.failed

    # 2) The written module.yaml carries the top-level mcp: block verbatim.
    mod_yaml_path = modules_dir / "example-remote-mcp" / "module.yaml"
    assert mod_yaml_path.is_file()
    written = yaml.safe_load(mod_yaml_path.read_text())
    assert "mcp" in written, "overlay must pass manifest.mcp through to module.yaml"
    assert written["mcp"]["example-remote"]["url"] == EXAMPLE_REMOTE_URL

    # 3) discover_modules reads it back into ModuleInfo.mcp_config.
    #    include_bundled=False keeps the assertion focused on our module and
    #    keeps the merged dict deterministic.
    modules = discover_modules(str(modules_dir), include_bundled=False)
    by_name = {m.name: m for m in modules}
    assert "example-remote-mcp" in by_name
    info = by_name["example-remote-mcp"]
    assert info.has_mcp is True
    assert info.mcp_config is not None

    # 4) collect_mcp_servers merges into the final dict handed to the agent
    #    runtime (Claude ClaudeAgentOptions.mcp_servers / Codex mcpServers).
    merged = collect_mcp_servers(modules)
    assert "example-remote" in merged
    assert merged["example-remote"]["url"] == EXAMPLE_REMOTE_URL


def test_names_only_header_placeholder_survives_verbatim(tmp_path: Path):
    """A ``${VAR}`` header placeholder is never resolved by agent-core.

    The overlay materialises it and the collector merges it, both leaving
    the literal ``${EXAMPLE_MCP_TOKEN}`` intact — value resolution is the
    server-side broker's job at boot, not the overlay's or collector's.
    """
    modules_dir = tmp_path / "modules"

    result = overlay_modules(
        [_example_remote_mcp_module_with_header()], str(modules_dir)
    )
    assert "example-remote-mcp-auth" in result.installed

    # The literal placeholder survives on disk — no value substitution.
    raw = (modules_dir / "example-remote-mcp-auth" / "module.yaml").read_text()
    assert "${EXAMPLE_MCP_TOKEN}" in raw

    modules = discover_modules(str(modules_dir), include_bundled=False)
    merged = collect_mcp_servers(modules)
    headers = merged["example-remote"]["headers"]
    # Verbatim placeholder — proves neither overlay nor collect resolve it.
    assert headers["Authorization"] == "${EXAMPLE_MCP_TOKEN}"


def test_collect_is_empty_without_mcp_module(tmp_path: Path):
    """Control: a module with no ``mcp`` block contributes nothing.

    Guards against the merged dict being populated by some ambient source —
    if this were non-empty, the positive assertions above could pass
    vacuously.
    """
    modules_dir = tmp_path / "modules"
    overlay_modules(
        [
            {
                "slug": "plain-mod",
                "name": "plain-mod",
                "manifest": {"description": "no mcp here", "env_keys": []},
                "files": [],
            }
        ],
        str(modules_dir),
    )
    modules = discover_modules(str(modules_dir), include_bundled=False)
    assert collect_mcp_servers(modules) == {}
