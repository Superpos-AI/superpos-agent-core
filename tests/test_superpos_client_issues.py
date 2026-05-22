"""Tests for the issue methods on SuperposClient.

Uses ``httpx.MockTransport`` to capture outbound requests without hitting
a real network.  Each test asserts both the URL/body the client sends
and the unwrapping it applies to the envelope.  Mirrors the style of
``test_superpos_client_knowledge.py``.
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


# ── list_issues ─────────────────────────────────────────────────────────


async def test_list_issues_default_no_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope(
            [{"id": "i1", "title": "T", "state": "open"}],
            meta={"per_page": 15, "current_page": 1, "has_more": False},
        )

    client = _make_client(handler)
    result = await client.list_issues()

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/issues"
    # No filters → no params
    assert dict(req.url.params) == {}
    # Returns full envelope (callers need meta for pagination)
    assert result["data"][0]["id"] == "i1"
    assert result["meta"]["has_more"] is False
    await client.close()


async def test_list_issues_passes_all_filters():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_issues(
        state="in_progress",
        issue_type_id="it-1",
        assignee_id="a-1",
        q="auth",
        per_page=25,
    )

    req = captured[0]
    assert req.url.params["state"] == "in_progress"
    assert req.url.params["issue_type_id"] == "it-1"
    assert req.url.params["assignee_id"] == "a-1"
    assert req.url.params["q"] == "auth"
    assert req.url.params["per_page"] == "25"
    await client.close()


# ── get_issue ───────────────────────────────────────────────────────────


async def test_get_issue_unwraps_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/issues/i1"
        return _envelope({"id": "i1", "title": "T", "state": "open"})

    client = _make_client(handler)
    issue = await client.get_issue("i1")
    assert issue == {"id": "i1", "title": "T", "state": "open"}
    await client.close()


# ── create_issue ────────────────────────────────────────────────────────


async def test_create_issue_sends_required_and_optional():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "i1", "title": "T", "state": "open"}, status=201)

    client = _make_client(handler)
    await client.create_issue(
        title="Webhook 502",
        issue_type_id="it-1",
        description="repro: ...",
        metadata={"severity": "high"},
        channel_id="c-1",
    )

    req = captured[0]
    body = json.loads(req.content)
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/issues"
    assert body == {
        "title": "Webhook 502",
        "issue_type_id": "it-1",
        "description": "repro: ...",
        "metadata": {"severity": "high"},
        "channel_id": "c-1",
    }
    # None-valued kwargs are not sent
    assert "assignee_type" not in body
    assert "thread_id" not in body
    await client.close()


# ── update_issue ────────────────────────────────────────────────────────


async def test_update_issue_only_sends_provided_fields():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "i1", "title": "new"})

    client = _make_client(handler)
    await client.update_issue("i1", title="new", metadata={"x": 1})

    req = captured[0]
    body = json.loads(req.content)
    assert req.method == "PATCH"
    assert req.url.path == "/api/v1/hives/hive-x/issues/i1"
    assert body == {"title": "new", "metadata": {"x": 1}}
    await client.close()


async def test_update_issue_rejects_empty_payload():
    """A `PATCH` with no fields is a caller bug — fail fast rather than
    hitting the server with an empty body."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _envelope({})

    client = _make_client(handler)
    with pytest.raises(ValueError, match="at least one field"):
        await client.update_issue("i1")
    assert not called
    await client.close()


# ── transition_issue ────────────────────────────────────────────────────


async def test_transition_issue_passes_to_and_reason():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "i1", "state": "in_progress"})

    client = _make_client(handler)
    await client.transition_issue("i1", to="in_progress", reason="picking up")

    req = captured[0]
    body = json.loads(req.content)
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/issues/i1/transition"
    assert body == {"to": "in_progress", "reason": "picking up"}
    await client.close()


async def test_transition_issue_omits_reason_when_unset():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "i1", "state": "done"})

    client = _make_client(handler)
    await client.transition_issue("i1", to="done")

    body = json.loads(captured[0].content)
    assert body == {"to": "done"}
    await client.close()


# ── close_issue ─────────────────────────────────────────────────────────


async def test_close_issue_with_reason():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "i1", "state": "done"})

    client = _make_client(handler)
    await client.close_issue("i1", reason="merged PR #42")

    req = captured[0]
    body = json.loads(req.content)
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/issues/i1/close"
    assert body == {"reason": "merged PR #42"}
    await client.close()


async def test_close_issue_no_reason_sends_no_body():
    """When no reason is given the JSON body should be omitted (sent as
    ``null``), not an empty dict — matches the existing pattern used by
    other no-body POSTs in the client."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "i1", "state": "done"})

    client = _make_client(handler)
    await client.close_issue("i1")

    # httpx serialises json=None to an empty (or absent) body.
    assert captured[0].content == b""
    await client.close()


# ── link helpers ────────────────────────────────────────────────────────


async def test_link_task_to_issue():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "t1", "issue_id": "i1"})

    client = _make_client(handler)
    await client.link_task_to_issue("i1", task_id="t1")

    req = captured[0]
    body = json.loads(req.content)
    assert req.url.path == "/api/v1/hives/hive-x/issues/i1/link-task"
    assert body == {"task_id": "t1"}
    await client.close()


async def test_link_channel_to_issue():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "i1"})

    client = _make_client(handler)
    await client.link_channel_to_issue("i1", channel_id="c-1")

    body = json.loads(captured[0].content)
    assert body == {"channel_id": "c-1"}
    await client.close()


# ── approvals & dependencies ────────────────────────────────────────────


async def test_request_issue_approval_full_payload():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "ar-1", "status": "pending"}, status=201)

    client = _make_client(handler)
    await client.request_issue_approval(
        "i1",
        summary="needs sign-off",
        recommended_action="approve_closure",
        risks="rollback requires redeploy",
        linked_issue_ids=["i2", "i3"],
    )

    req = captured[0]
    body = json.loads(req.content)
    assert req.url.path == "/api/v1/hives/hive-x/issues/i1/request-approval"
    assert body == {
        "summary": "needs sign-off",
        "recommended_action": "approve_closure",
        "risks": "rollback requires redeploy",
        "linked_issue_ids": ["i2", "i3"],
    }
    await client.close()


async def test_create_issue_dependency():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "d-1", "kind": "blocks"}, status=201)

    client = _make_client(handler)
    await client.create_issue_dependency(
        "i1", depends_on_issue_id="i2", kind="blocks",
    )

    req = captured[0]
    body = json.loads(req.content)
    assert req.url.path == "/api/v1/hives/hive-x/issues/i1/dependencies"
    assert body == {"depends_on_issue_id": "i2", "kind": "blocks"}
    await client.close()


async def test_delete_issue_dependency_no_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    client = _make_client(handler)
    await client.delete_issue_dependency("i1", "d-1")

    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/hives/hive-x/issues/i1/dependencies/d-1"
    await client.close()


# ── issue types ─────────────────────────────────────────────────────────


async def test_list_issue_types_unwraps_data():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/issue-types"
        return _envelope([{"id": "it-1", "key": "bug"}])

    client = _make_client(handler)
    types = await client.list_issue_types()
    assert types == [{"id": "it-1", "key": "bug"}]
    await client.close()
