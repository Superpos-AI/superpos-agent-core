"""Tests for SuperposClient.get_registry_resolved (Beat 2b SDK method).

Uses ``httpx.MockTransport`` to assert the URL the client hits and the
envelope unwrapping it applies, plus the defensive ``None`` fall-backs.
"""

from __future__ import annotations

import httpx

from superpos_agent_core import BaseConfig, SuperposClient


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


async def test_get_registry_resolved_hits_endpoint_and_unwraps():
    captured: list[httpx.Request] = []

    payload = {
        "items": [],
        "skills": [{"slug": "deep-research", "instructions": "# x"}],
        "modules": [{"slug": "superpos-github", "manifest": {}}],
        "subagents": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": payload})

    client = _make_client(handler)
    result = await client.get_registry_resolved()

    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/api/v1/registry/resolved"
    assert result == payload
    assert [s["slug"] for s in result["skills"]] == ["deep-research"]


async def test_get_registry_resolved_accepts_unwrapped_body():
    payload = {"skills": [], "modules": []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _make_client(handler)
    result = await client.get_registry_resolved()
    assert result == payload


async def test_get_registry_resolved_returns_none_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "nope"})

    client = _make_client(handler)
    assert await client.get_registry_resolved() is None


async def test_get_registry_resolved_returns_none_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _make_client(handler)
    assert await client.get_registry_resolved() is None
