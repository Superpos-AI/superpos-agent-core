"""Tests for the registry *authoring* (write) methods on SuperposClient.

Covers create / update / delete / show / list and the kind validation that
guards every one of them. Uses ``httpx.MockTransport`` to capture the outbound
request (method, path, params, body) and assert the envelope unwrapping —
mirroring the style of ``test_superpos_client_registry.py`` and
``test_superpos_client_tracks.py``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from superpos_agent_core import REGISTRY_KINDS, BaseConfig, SuperposClient


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


def _envelope(data, status=200):
    return httpx.Response(status, json={"data": data})


# ── kinds constant ───────────────────────────────────────────────────────


def test_registry_kinds_match_server():
    assert REGISTRY_KINDS == ("subagent", "skill", "module", "dynamic_workflow")


# ── create ───────────────────────────────────────────────────────────────


async def test_create_registry_item_posts_to_kind_with_slug_in_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "r1", "kind": "skill", "slug": "deep-dive"}, status=201)

    client = _make_client(handler)
    payload = {"frontmatter": {"name": "Deep Dive"}, "instructions": "# x", "files": []}
    result = await client.create_registry_item(
        "skill",
        "deep-dive",
        name="Deep Dive",
        payload=payload,
        description="thorough",
        visibility="private",
        owner_agent_id="agent-x",
        message="initial",
    )

    req = captured[0]
    assert req.method == "POST"
    # Slug travels in the body, not the path (POST /registry/{kind}).
    assert req.url.path == "/api/v1/registry/skill"
    body = json.loads(req.content)
    assert body == {
        "slug": "deep-dive",
        "name": "Deep Dive",
        "payload": payload,
        "description": "thorough",
        "visibility": "private",
        "owner_agent_id": "agent-x",
        "message": "initial",
    }
    assert result == {"id": "r1", "kind": "skill", "slug": "deep-dive"}
    await client.close()


async def test_create_registry_item_omits_unset_optionals():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "r2"}, status=201)

    client = _make_client(handler)
    await client.create_registry_item(
        "module", "superpos-foo", name="Foo", payload={"manifest": {}},
    )

    body = json.loads(captured[0].content)
    assert body == {"slug": "superpos-foo", "name": "Foo", "payload": {"manifest": {}}}
    # Optional keys must be absent, not null.
    for k in ("description", "visibility", "owner_agent_id", "message"):
        assert k not in body
    await client.close()


async def test_create_registry_item_rejects_bad_kind():
    client = _make_client(lambda r: _envelope({}))
    with pytest.raises(ValueError, match="unknown registry kind 'plugin'"):
        await client.create_registry_item("plugin", "x", name="X", payload={})
    await client.close()


# ── update ───────────────────────────────────────────────────────────────


async def test_update_registry_item_patches_kind_slug():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "r1", "is_active": True})

    client = _make_client(handler)
    result = await client.update_registry_item(
        "skill",
        "deep-dive",
        name="Deeper Dive",
        payload={"instructions": "# y"},
        is_active=True,
        visibility="hive",
        message="bump",
    )

    req = captured[0]
    assert req.method == "PATCH"
    assert req.url.path == "/api/v1/registry/skill/deep-dive"
    body = json.loads(req.content)
    assert body == {
        "name": "Deeper Dive",
        "payload": {"instructions": "# y"},
        "is_active": True,
        "visibility": "hive",
        "message": "bump",
    }
    assert result == {"id": "r1", "is_active": True}
    await client.close()


async def test_update_registry_item_sends_only_provided_fields():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "r1"})

    client = _make_client(handler)
    # is_active=False must be SENT (it's a meaningful toggle, not "unset").
    await client.update_registry_item("subagent", "agent-a", is_active=False)

    body = json.loads(captured[0].content)
    assert body == {"is_active": False}
    await client.close()


async def test_update_registry_item_rejects_bad_kind():
    client = _make_client(lambda r: _envelope({}))
    with pytest.raises(ValueError, match="unknown registry kind"):
        await client.update_registry_item("widget", "x", name="X")
    await client.close()


# ── delete ───────────────────────────────────────────────────────────────


async def test_delete_registry_item_issues_delete():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    client = _make_client(handler)
    result = await client.delete_registry_item("dynamic_workflow", "nightly")

    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/registry/dynamic_workflow/nightly"
    assert result is None
    await client.close()


async def test_delete_registry_item_rejects_bad_kind():
    client = _make_client(lambda r: _envelope({}))
    with pytest.raises(ValueError, match="unknown registry kind"):
        await client.delete_registry_item("nope", "x")
    await client.close()


# ── show / list ──────────────────────────────────────────────────────────


async def test_get_registry_item_unwraps_envelope():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "r1", "slug": "deep-dive", "payload": {"a": 1}})

    client = _make_client(handler)
    result = await client.get_registry_item("skill", "deep-dive")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/registry/skill/deep-dive"
    assert result == {"id": "r1", "slug": "deep-dive", "payload": {"a": 1}}
    await client.close()


async def test_list_registry_items_default_no_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([{"slug": "a"}, {"slug": "b"}])

    client = _make_client(handler)
    items = await client.list_registry_items("skill")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/registry/skill"
    assert dict(req.url.params) == {}
    assert [i["slug"] for i in items] == ["a", "b"]
    await client.close()


async def test_list_registry_items_forwards_include_flags():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_registry_items(
        "module", include_inactive=True, include_deleted=True,
    )

    params = dict(captured[0].url.params)
    assert params == {"include_inactive": "true", "include_deleted": "true"}
    await client.close()


async def test_list_registry_items_rejects_bad_kind():
    client = _make_client(lambda r: _envelope([]))
    with pytest.raises(ValueError, match="unknown registry kind"):
        await client.list_registry_items("bogus")
    await client.close()
