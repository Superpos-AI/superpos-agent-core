"""Tests for SuperposClient.update_task and the typed Task resource (issue #97).

Uses ``httpx.MockTransport`` to capture outbound requests without hitting
a real network — same pattern as ``test_superpos_client_knowledge.py``.
Asserts the PATCH URL + body, the conditional ``X-Audit-Reason`` header,
envelope unwrapping, and that 422/409/429 propagate as
``httpx.HTTPStatusError``.

The SuperposClient in this package is async-only (a single
``httpx.AsyncClient``); there is no separate sync client class, so the
"sync+async" coverage the issue calls for is: the async client method
here, plus the synchronous ``superpos-task update`` CLI path covered in
``test_superpos_task_update_cli.py``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from superpos_agent_core import BaseConfig, SuperposClient, Task

HIVE = "hive-x"
BASE = f"/api/v1/hives/{HIVE}/tasks"


def _make_client(handler):
    config = BaseConfig(
        superpos_base_url="https://test.example",
        superpos_hive_id=HIVE,
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
    return httpx.Response(status, json={"data": data, "meta": {}, "errors": []})


def _capturing(response):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    return handler, captured


# ── update_task ───────────────────────────────────────────────────────


async def test_update_task_patches_right_url_with_fields_and_unwraps():
    handler, captured = _capturing(_envelope({"id": "01HX", "priority": 4}))
    client = _make_client(handler)

    result = await client.update_task("01HX", fields={"priority": 4})

    req = captured[0]
    assert req.method == "PATCH"
    assert req.url.path == f"{BASE}/01HX"
    assert json.loads(req.content) == {"priority": 4}
    assert result == {"id": "01HX", "priority": 4}


async def test_update_task_adds_audit_reason_header_when_given():
    handler, captured = _capturing(_envelope({"id": "01HX"}))
    client = _make_client(handler)

    await client.update_task(
        "01HX",
        fields={"target_agent_id": None},
        audit_reason="wrong target, redirecting",
    )

    req = captured[0]
    assert req.headers["X-Audit-Reason"] == "wrong target, redirecting"
    # explicit null is preserved in the body, not dropped
    assert json.loads(req.content) == {"target_agent_id": None}


async def test_update_task_omits_audit_reason_header_when_absent():
    handler, captured = _capturing(_envelope({"id": "01HX"}))
    client = _make_client(handler)

    await client.update_task("01HX", fields={"priority": 1})

    assert "x-audit-reason" not in captured[0].headers


async def test_update_task_hive_override():
    handler, captured = _capturing(_envelope({"id": "01HX"}))
    client = _make_client(handler)

    await client.update_task("01HX", fields={"priority": 1}, hive_id="other-hive")

    assert captured[0].url.path == "/api/v1/hives/other-hive/tasks/01HX"


@pytest.mark.parametrize("status", [409, 422, 429])
async def test_update_task_propagates_errors(status):
    handler, _ = _capturing(_envelope({"errors": [{"field": "id"}]}, status=status))
    client = _make_client(handler)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await client.update_task("01HX", fields={"priority": 4})
    assert exc.value.response.status_code == status


# ── Task resource ──────────────────────────────────────────────────────


async def test_task_update_delegates_with_fields_and_audit_reason():
    handler, captured = _capturing(_envelope({"id": "01HX", "target_agent_id": None}))
    client = _make_client(handler)

    task = Task(client, "01HX")
    result = await task.update(target_agent_id=None, audit_reason="redirecting")

    req = captured[0]
    assert req.method == "PATCH"
    assert req.url.path == f"{BASE}/01HX"
    assert json.loads(req.content) == {"target_agent_id": None}
    assert req.headers["X-Audit-Reason"] == "redirecting"
    assert result == {"id": "01HX", "target_agent_id": None}


async def test_task_update_passes_multiple_fields():
    handler, captured = _capturing(_envelope({"id": "01HX"}))
    client = _make_client(handler)

    await Task(client, "01HX").update(priority=4, target_capability="data-analysis")

    assert json.loads(captured[0].content) == {
        "priority": 4,
        "target_capability": "data-analysis",
    }


async def test_task_update_requires_a_field():
    client = _make_client(lambda r: _envelope({}))
    with pytest.raises(ValueError):
        await Task(client, "01HX").update()
