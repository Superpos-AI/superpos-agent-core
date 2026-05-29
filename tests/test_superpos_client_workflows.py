"""Tests for the workflow methods on SuperposClient.

Uses ``httpx.MockTransport`` to capture outbound requests without hitting
a real network.  Mirrors ``test_superpos_client_issues.py``.
"""

from __future__ import annotations

import json

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


# ── list_workflows ─────────────────────────────────────────────────────


async def test_list_workflows_default_no_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope(
            [{"id": "w1", "slug": "pr-review", "name": "PR Review"}],
            meta={"per_page": 15, "current_page": 1, "has_more": False},
        )

    client = _make_client(handler)
    result = await client.list_workflows()

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/workflows"
    assert dict(req.url.params) == {}
    assert result["data"][0]["slug"] == "pr-review"
    assert result["meta"]["has_more"] is False
    await client.close()


async def test_list_workflows_passes_all_filters():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_workflows(active=True, q="pr", page=2, per_page=25)

    req = captured[0]
    assert req.url.params["active"] == "true"
    assert req.url.params["q"] == "pr"
    assert req.url.params["page"] == "2"
    assert req.url.params["per_page"] == "25"
    await client.close()


async def test_list_workflows_active_false_serialises():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_workflows(active=False)

    assert captured[0].url.params["active"] == "false"
    await client.close()


# ── get_workflow ───────────────────────────────────────────────────────


async def test_get_workflow_by_slug_unwraps_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/workflows/pr-review"
        return _envelope({"id": "w1", "slug": "pr-review"})

    client = _make_client(handler)
    wf = await client.get_workflow("pr-review")
    assert wf == {"id": "w1", "slug": "pr-review"}
    await client.close()


# ── create_workflow ────────────────────────────────────────────────────


async def test_create_workflow_sends_required_and_optional():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "w1"}, status=201)

    client = _make_client(handler)
    await client.create_workflow(
        name="PR Review",
        slug="pr-review",
        trigger_config={"type": "manual"},
        steps=[{"key": "s1", "type": "agent"}],
        description="Reviews PRs",
        settings={"max_runs": 5},
    )

    req = captured[0]
    body = json.loads(req.content)
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/workflows"
    assert body == {
        "name": "PR Review",
        "slug": "pr-review",
        "trigger_config": {"type": "manual"},
        "steps": [{"key": "s1", "type": "agent"}],
        "is_active": True,
        "description": "Reviews PRs",
        "settings": {"max_runs": 5},
    }
    await client.close()


async def test_create_workflow_inactive_flag():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "w1"}, status=201)

    client = _make_client(handler)
    await client.create_workflow(
        name="WF",
        slug="wf",
        trigger_config={"type": "manual"},
        steps=[],
        is_active=False,
    )

    body = json.loads(captured[0].content)
    assert body["is_active"] is False
    assert "description" not in body
    assert "settings" not in body
    await client.close()


# ── update_workflow ────────────────────────────────────────────────────


async def test_update_workflow_only_sends_provided_fields():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "w1", "name": "new"})

    client = _make_client(handler)
    await client.update_workflow("w1", name="new", steps=[{"key": "s1"}])

    req = captured[0]
    body = json.loads(req.content)
    assert req.method == "PUT"
    assert req.url.path == "/api/v1/hives/hive-x/workflows/w1"
    assert body == {"name": "new", "steps": [{"key": "s1"}]}
    await client.close()


async def test_update_workflow_rejects_empty_payload():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _envelope({})

    client = _make_client(handler)
    with pytest.raises(ValueError, match="at least one field"):
        await client.update_workflow("w1")
    assert not called
    await client.close()


async def test_update_workflow_is_active_false_round_trips():
    """``is_active=False`` must reach the server — a naive ``if value``
    check would drop it as falsy."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "w1"})

    client = _make_client(handler)
    await client.update_workflow("w1", is_active=False)

    body = json.loads(captured[0].content)
    assert body == {"is_active": False}
    await client.close()


# ── delete_workflow ────────────────────────────────────────────────────


async def test_delete_workflow_no_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    client = _make_client(handler)
    await client.delete_workflow("w1")

    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/hives/hive-x/workflows/w1"
    await client.close()


# ── versions ──────────────────────────────────────────────────────────


async def test_list_workflow_versions_unwraps_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/workflows/w1/versions"
        return _envelope([{"version": 2}, {"version": 1}])

    client = _make_client(handler)
    versions = await client.list_workflow_versions("w1")
    assert versions == [{"version": 2}, {"version": 1}]
    await client.close()


async def test_get_workflow_version_returns_snapshot():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/workflows/w1/versions/3"
        return _envelope({"version": 3, "steps": []})

    client = _make_client(handler)
    snap = await client.get_workflow_version("w1", 3)
    assert snap == {"version": 3, "steps": []}
    await client.close()


async def test_diff_workflow_versions_path_and_method():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == (
            "/api/v1/hives/hive-x/workflows/w1/versions/2/diff/3"
        )
        return _envelope({"changes": []})

    client = _make_client(handler)
    diff = await client.diff_workflow_versions("w1", 2, 3)
    assert diff == {"changes": []}
    await client.close()


async def test_rollback_workflow_version():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"version": 4, "rolled_back_from": 2})

    client = _make_client(handler)
    result = await client.rollback_workflow_version("w1", 2)

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == (
        "/api/v1/hives/hive-x/workflows/w1/versions/2/rollback"
    )
    assert result == {"version": 4, "rolled_back_from": 2}
    await client.close()


# ── runs ──────────────────────────────────────────────────────────────


async def test_list_workflow_runs_default_no_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope(
            [{"id": "r1", "status": "running"}],
            meta={"current_page": 1, "has_more": False},
        )

    client = _make_client(handler)
    result = await client.list_workflow_runs("w1")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/workflows/w1/runs"
    assert dict(req.url.params) == {}
    assert result["data"][0]["id"] == "r1"
    assert result["meta"]["has_more"] is False
    await client.close()


async def test_list_workflow_runs_with_filters():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_workflow_runs("w1", status="failed", page=3, per_page=10)

    params = captured[0].url.params
    assert params["status"] == "failed"
    assert params["page"] == "3"
    assert params["per_page"] == "10"
    await client.close()


async def test_get_workflow_run_unwraps_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/workflows/w1/runs/r1"
        return _envelope({
            "id": "r1",
            "status": "running",
            "thread": {"id": "t1"},
            "step_states": [{"key": "s1", "state": "completed"}],
        })

    client = _make_client(handler)
    run = await client.get_workflow_run("w1", "r1")
    assert run["thread"]["id"] == "t1"
    assert run["step_states"][0]["state"] == "completed"
    await client.close()


async def test_start_workflow_run_with_payload():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "r1", "status": "running"}, status=201)

    client = _make_client(handler)
    await client.start_workflow_run("w1", payload={"pr_number": 42})

    req = captured[0]
    body = json.loads(req.content)
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/workflows/w1/runs"
    assert body == {"payload": {"pr_number": 42}}
    await client.close()


async def test_start_workflow_run_without_payload_sends_empty_object():
    """The server still requires a JSON body, so the client must send
    ``{}`` rather than ``null``."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "r1", "status": "running"}, status=201)

    client = _make_client(handler)
    await client.start_workflow_run("w1")

    body = json.loads(captured[0].content)
    assert body == {}
    await client.close()


async def test_cancel_workflow_run():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "r1", "status": "cancelled"})

    client = _make_client(handler)
    result = await client.cancel_workflow_run("w1", "r1")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/workflows/w1/runs/r1/cancel"
    assert result["status"] == "cancelled"
    await client.close()


async def test_retry_workflow_run():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "r1", "status": "retrying"})

    client = _make_client(handler)
    result = await client.retry_workflow_run("w1", "r1")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/workflows/w1/runs/r1/retry"
    assert result["status"] == "retrying"
    await client.close()
