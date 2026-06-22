"""Tests for the hosted-agent methods on SuperposClient.

Uses ``httpx.MockTransport`` to capture outbound requests without hitting
a real network.  Each test asserts both the URL/body the client sends
and the unwrapping it applies to the envelope.  Mirrors the style of
``test_superpos_client_tracks.py``.

Hosted agents are a Cloud-only feature; these tests exercise the SDK
surface regardless of edition (the mock transport answers every route).
"""

from __future__ import annotations

import json

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


def _envelope(data, meta=None, status=200):
    body = {"data": data}
    if meta is not None:
        body["meta"] = meta
    return httpx.Response(status, json=body)


# ── list_hosted_agents ───────────────────────────────────────────────────


async def test_list_hosted_agents_hits_index_returns_envelope():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope(
            [{"id": "h1", "status": "running"}],
            meta={"pagination": {"total": 1, "current_page": 1}},
        )

    client = _make_client(handler)
    out = await client.list_hosted_agents()

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents"
    assert dict(req.url.params) == {}
    # Full envelope preserved so callers can paginate via meta.pagination.
    assert out["data"] == [{"id": "h1", "status": "running"}]
    assert out["meta"]["pagination"]["total"] == 1
    await client.close()


async def test_list_hosted_agents_forwards_pagination():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([], meta={"pagination": {}})

    client = _make_client(handler)
    await client.list_hosted_agents(page=2, per_page=50)

    req = captured[0]
    assert req.url.params["page"] == "2"
    assert req.url.params["per_page"] == "50"
    await client.close()


# ── get_hosted_agent ─────────────────────────────────────────────────────


async def test_get_hosted_agent_unwraps_data():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "h1", "status": "running", "model": "claude"})

    client = _make_client(handler)
    out = await client.get_hosted_agent("h1")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1"
    assert out == {"id": "h1", "status": "running", "model": "claude"}
    await client.close()


# ── get_hosted_agent_status ──────────────────────────────────────────────


async def test_get_hosted_agent_status_unwraps_data():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"status": "running", "novps_status": "Running",
                          "checked_at": "2026-06-22T00:00:00Z"})

    client = _make_client(handler)
    out = await client.get_hosted_agent_status("h1")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1/status"
    assert out["novps_status"] == "Running"
    await client.close()


# ── get_hosted_agent_logs ────────────────────────────────────────────────


async def test_get_hosted_agent_logs_forwards_all_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"lines": ["a", "b"]}, meta={"source": "novps"})

    client = _make_client(handler)
    out = await client.get_hosted_agent_logs(
        "h1",
        start="2026-06-22T10:00:00Z",
        end="2026-06-22T11:00:00Z",
        limit=200,
        direction="backward",
        search="error",
        pod="pod-1",
    )

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1/logs"
    params = dict(req.url.params)
    assert params == {
        "start": "2026-06-22T10:00:00Z",
        "end": "2026-06-22T11:00:00Z",
        "limit": "200",
        "direction": "backward",
        "search": "error",
        "pod": "pod-1",
    }
    # Full envelope preserved (meta.source carries the upstream marker).
    assert out["meta"]["source"] == "novps"
    await client.close()


async def test_get_hosted_agent_logs_omits_unset_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"lines": []})

    client = _make_client(handler)
    await client.get_hosted_agent_logs("h1")

    req = captured[0]
    assert dict(req.url.params) == {}
    await client.close()


# ── list_hosted_agent_deployments ────────────────────────────────────────


async def test_list_hosted_agent_deployments_returns_envelope():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope(
            [{"id": "d1", "status": "success"}],
            meta={"pagination": {"current_page": 1}},
        )

    client = _make_client(handler)
    out = await client.list_hosted_agent_deployments("h1", page=1, per_page=10)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1/deployments"
    assert req.url.params["page"] == "1"
    assert req.url.params["per_page"] == "10"
    assert out["data"][0]["id"] == "d1"
    await client.close()


# ── start / stop / restart / redeploy (no-body POST verbs) ───────────────


async def test_start_hosted_agent_posts_to_start():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "h1", "status": "deploying"},
                         meta={"queued_job": "DeployHostedAgentJob"}, status=202)

    client = _make_client(handler)
    out = await client.start_hosted_agent("h1")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1/start"
    assert req.content in (b"", b"null")  # no JSON body sent
    assert out["meta"]["queued_job"] == "DeployHostedAgentJob"
    await client.close()


async def test_stop_hosted_agent_posts_to_stop():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "h1"}, meta={"queued_job": "StopHostedAgentJob"}, status=202)

    client = _make_client(handler)
    await client.stop_hosted_agent("h1")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1/stop"
    await client.close()


async def test_restart_hosted_agent_posts_to_restart():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "h1"}, status=202)

    client = _make_client(handler)
    await client.restart_hosted_agent("h1")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1/restart"
    await client.close()


async def test_redeploy_hosted_agent_posts_to_redeploy():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "h1"}, status=202)

    client = _make_client(handler)
    await client.redeploy_hosted_agent("h1")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1/redeploy"
    await client.close()


# ── scale ────────────────────────────────────────────────────────────────


async def test_scale_hosted_agent_sends_replicas_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "h1"}, status=202)

    client = _make_client(handler)
    await client.scale_hosted_agent("h1", size="m", count=3)

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1/scale"
    body = json.loads(req.content)
    assert body == {"replicas": {"size": "m", "count": 3}}
    await client.close()


# ── rollback ─────────────────────────────────────────────────────────────


async def test_rollback_hosted_agent_deployment_posts_to_nested_route():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "h1", "target_deployment_id": "d9"}, status=202)

    client = _make_client(handler)
    out = await client.rollback_hosted_agent_deployment("h1", "d9")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1/deployments/d9/rollback"
    assert out["data"]["target_deployment_id"] == "d9"
    await client.close()


# ── delete ───────────────────────────────────────────────────────────────


async def test_delete_hosted_agent_sends_delete():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "h1", "status": "deleting"},
                         meta={"queued_job": "DestroyHostedAgentJob"}, status=202)

    client = _make_client(handler)
    out = await client.delete_hosted_agent("h1")

    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/hives/hive-x/hosted-agents/h1"
    assert out["meta"]["queued_job"] == "DestroyHostedAgentJob"
    await client.close()


# ── presets (org-scoped, NOT hive-prefixed) ──────────────────────────────


async def test_list_hosted_agent_presets_is_org_scoped():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([{"key": "claude-sdk", "provider": "anthropic"}])

    client = _make_client(handler)
    out = await client.list_hosted_agent_presets()

    req = captured[0]
    assert req.method == "GET"
    # Critically: NOT under /hives/{hive}/ — this is the only org-scoped route.
    assert req.url.path == "/api/v1/hosted-agent-presets"
    assert "/hives/" not in req.url.path
    assert out == [{"key": "claude-sdk", "provider": "anthropic"}]
    await client.close()


# ── Auth header is sent on hosted-agent requests ─────────────────────────


async def test_auth_header_sent_on_hosted_agent_request():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_hosted_agents()

    auth = captured[0].headers.get("authorization", "")
    assert auth.startswith("Bearer ")
    assert auth.endswith("tok")
    await client.close()
