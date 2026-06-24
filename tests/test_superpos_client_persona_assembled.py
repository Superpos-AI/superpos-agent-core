"""Tests for SuperposClient.get_persona_assembled outage vs reachable-empty.

The AG-10 persona overlay (PR #53) must tell a genuine outage from a reachable
but empty / cleared persona, otherwise a cleared persona resurrects the stale
snapshot.  The client signals this by *raising* PersonaFetchUnavailable on an
outage and *returning* (str | None) when reachable — including a 404, which the
server uses for the reachable "no active persona for this agent" state.
"""

from __future__ import annotations

import httpx
import pytest

from superpos_agent_core import BaseConfig, SuperposClient
from superpos_agent_core.persona_overlay import PersonaFetchUnavailable


def _make_client(handler):
    config = BaseConfig(
        superpos_base_url="https://test.example",
        superpos_hive_id="hive-x",
        superpos_agent_id="agent-x",
        superpos_api_token="tok",
    )
    client = SuperposClient(config)
    client._client = httpx.AsyncClient(
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),
    )
    return client


async def test_persona_assembled_returns_content():
    client = _make_client(
        lambda req: httpx.Response(200, json={"data": {"prompt": "HELLO"}})
    )
    assert await client.get_persona_assembled() == "HELLO"


async def test_persona_assembled_reachable_empty_prompt_returns_none():
    """200 with a blank/missing prompt → None (reachable-empty), not a raise."""
    client = _make_client(
        lambda req: httpx.Response(200, json={"data": {"prompt": ""}})
    )
    assert await client.get_persona_assembled() is None


async def test_persona_assembled_404_is_reachable_empty_returns_none():
    """A 404 is the server's reachable "no active persona" state → None, not a raise.

    Mirrors the server contract in PersonaController::assembled() /
    PersonaSdkApiTest::test_assembled_returns_404_when_no_persona.  Raising here
    would push outage=True into apply_persona_overlay and resurrect the stale
    snapshot instead of clearing it.
    """
    client = _make_client(
        lambda req: httpx.Response(404, json={"error": "No active persona for this agent."})
    )
    assert await client.get_persona_assembled() is None


async def test_persona_assembled_server_error_raises():
    """A genuine outage (5xx) still raises so callers serve the snapshot."""
    client = _make_client(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(PersonaFetchUnavailable):
        await client.get_persona_assembled()


async def test_persona_assembled_transport_error_raises():
    """A transport error is an outage → raise (overlay serves the snapshot)."""

    def _boom(req):
        raise httpx.ConnectError("no route to host")

    client = _make_client(_boom)
    with pytest.raises(PersonaFetchUnavailable):
        await client.get_persona_assembled()
