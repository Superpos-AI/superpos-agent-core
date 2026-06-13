"""Tests for ``SuperposClient.list_tasks``.

Uses ``httpx.MockTransport`` to capture the outbound request without a real
network. Mirrors the style of ``test_superpos_client_tracks.py`` /
``test_superpos_client_issues.py``: each test asserts both the URL/query the
client sends and the envelope unwrapping it applies.
"""

from __future__ import annotations

import httpx
import pytest

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


def _envelope(data, meta=None, status=200):
    body = {"data": data}
    if meta is not None:
        body["meta"] = meta
    return httpx.Response(status, json=body)


async def test_list_tasks_no_filters_hits_index_no_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([{"id": "t1", "type": "default", "status": "queued"}])

    client = _make_client(handler)
    tasks = await client.list_tasks("hive-x")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/tasks"
    # No filters → no query params at all (None-valued filters are omitted).
    assert dict(req.url.params) == {}
    # Envelope ``data`` is unwrapped.
    assert tasks == [{"id": "t1", "type": "default", "status": "queued"}]
    await client.close()


async def test_list_tasks_uses_explicit_hive_id():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_tasks("other-hive")

    assert captured[0].url.path == "/api/v1/hives/other-hive/tasks"
    await client.close()


async def test_list_tasks_only_non_none_filters_become_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_tasks(
        "hive-x",
        status="completed",
        type="default",
        target_agent_id="agent-7",
        target_capability="coding",
        creator_id="creator-1",
        parent_task_id="parent-9",
        created_after="2026-01-01T00:00:00Z",
        created_before="2026-12-31T00:00:00Z",
        q="search me",
    )

    params = dict(captured[0].url.params)
    assert params == {
        "status": "completed",
        "type": "default",
        "target_agent_id": "agent-7",
        "target_capability": "coding",
        "creator_id": "creator-1",
        "parent_task_id": "parent-9",
        "created_after": "2026-01-01T00:00:00Z",
        "created_before": "2026-12-31T00:00:00Z",
        "q": "search me",
    }
    await client.close()


async def test_list_tasks_partial_filters_omit_none():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_tasks("hive-x", status="failed", q=None, type=None)

    params = dict(captured[0].url.params)
    assert params == {"status": "failed"}
    await client.close()


async def test_list_tasks_passes_pagination_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope(
            [{"id": "t1"}],
            meta={"current_page": 2, "has_more": False},
        )

    client = _make_client(handler)
    tasks = await client.list_tasks("hive-x", page=2, per_page=50)

    params = dict(captured[0].url.params)
    assert params == {"page": "2", "per_page": "50"}
    assert tasks == [{"id": "t1"}]
    await client.close()


async def test_list_tasks_handles_bare_list_body():
    """If the server returns a bare list (no envelope), it is returned as-is."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "t1"}])

    client = _make_client(handler)
    tasks = await client.list_tasks("hive-x")
    assert tasks == [{"id": "t1"}]
    await client.close()
