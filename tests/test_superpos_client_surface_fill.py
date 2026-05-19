"""Tests for the surface-fill methods on SuperposClient.

Covers knowledge writes, sub-agents, service proxy, drain mode, task
tracing/replay, dead-letter, threads, schedule pause/resume, and event
publish.  Same httpx.MockTransport pattern as the knowledge read tests:
each test asserts both the outbound request shape and the unwrapping
the client applies.
"""

from __future__ import annotations

from typing import Any

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


def _envelope(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


def _raw(data: Any) -> httpx.Response:
    return httpx.Response(200, json=data)


def _empty_204() -> httpx.Response:
    return httpx.Response(204)


# ══ Knowledge writes ═══════════════════════════════════════════════════════


async def test_create_knowledge_sends_required_fields():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "k1", "key": "deploy.staging", "version": 1})

    client = _make_client(handler)
    out = await client.create_knowledge(key="deploy.staging", value={"x": 1})

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/knowledge"
    body = httpx_json(req)
    assert body == {"key": "deploy.staging", "value": {"x": 1}}
    assert out["id"] == "k1"
    await client.close()


async def test_create_knowledge_optional_fields_omitted_when_none():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "k1"})

    client = _make_client(handler)
    await client.create_knowledge(key="k", value=1)
    body = httpx_json(captured[0])
    for absent in ("scope", "visibility", "ttl"):
        assert absent not in body
    await client.close()


async def test_create_knowledge_optional_fields_passed_through():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "k1"})

    client = _make_client(handler)
    await client.create_knowledge(
        key="k", value="v", scope="agent:42",
        visibility="private", ttl="P30D",
    )
    body = httpx_json(captured[0])
    assert body["scope"] == "agent:42"
    assert body["visibility"] == "private"
    assert body["ttl"] == "P30D"
    await client.close()


async def test_update_knowledge_sends_value_only_by_default():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "k1", "version": 2})

    client = _make_client(handler)
    await client.update_knowledge("01ABC", value="new")

    req = captured[0]
    assert req.method == "PUT"
    assert req.url.path == "/api/v1/hives/hive-x/knowledge/01ABC"
    assert httpx_json(req) == {"value": "new"}
    await client.close()


async def test_delete_knowledge_returns_none():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _empty_204()

    client = _make_client(handler)
    result = await client.delete_knowledge("01ABC")

    assert result is None
    assert captured[0].method == "DELETE"
    assert captured[0].url.path == "/api/v1/hives/hive-x/knowledge/01ABC"
    await client.close()


async def test_list_knowledge_links_filters_and_omits_none():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([{"id": "L1"}])

    client = _make_client(handler)
    await client.list_knowledge_links(source_id="01ABC", target_type="agent")

    req = captured[0]
    assert req.url.path == "/api/v1/hives/hive-x/knowledge/links"
    assert req.url.params["source"] == "01ABC"
    assert req.url.params["target_type"] == "agent"
    assert "target" not in req.url.params  # not provided
    assert "limit" not in req.url.params
    await client.close()


async def test_create_knowledge_link_defaults():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "L1", "link_type": "relates_to"})

    client = _make_client(handler)
    await client.create_knowledge_link("01ABC", target_id="02DEF")

    req = captured[0]
    assert req.url.path == "/api/v1/hives/hive-x/knowledge/01ABC/links"
    body = httpx_json(req)
    assert body == {
        "target_type": "knowledge",
        "link_type": "relates_to",
        "target_id": "02DEF",
    }
    await client.close()


async def test_create_knowledge_link_external_target_ref():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "L2"})

    client = _make_client(handler)
    await client.create_knowledge_link(
        "01ABC",
        target_ref="https://github.com/x/y/issues/3",
        target_type="external",
        link_type="references",
        metadata={"note": "ticket"},
    )
    body = httpx_json(captured[0])
    assert body["target_ref"] == "https://github.com/x/y/issues/3"
    assert body["target_type"] == "external"
    assert body["link_type"] == "references"
    assert body["metadata"] == {"note": "ticket"}
    await client.close()


async def test_delete_knowledge_link():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _empty_204()

    client = _make_client(handler)
    await client.delete_knowledge_link("LINK1")
    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/hives/hive-x/knowledge/links/LINK1"
    await client.close()


async def test_confirm_and_dismiss_knowledge_link():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "LINK1", "status": "confirmed"})

    client = _make_client(handler)
    await client.confirm_knowledge_link("LINK1")
    assert captured[-1].method == "POST"
    assert captured[-1].url.path.endswith("/links/LINK1/confirm")

    await client.dismiss_knowledge_link("LINK1")
    assert captured[-1].method == "DELETE"
    assert captured[-1].url.path.endswith("/links/LINK1/dismiss")
    await client.close()


# ══ Sub-agents ═════════════════════════════════════════════════════════════


async def test_list_sub_agents():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/sub-agents"
        return _envelope([{"slug": "code-reviewer", "id": "01XYZ"}])

    client = _make_client(handler)
    out = await client.list_sub_agents()
    assert out == [{"slug": "code-reviewer", "id": "01XYZ"}]
    await client.close()


async def test_get_sub_agent_by_slug_and_by_id():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"slug": "code-reviewer", "version": 3})

    client = _make_client(handler)
    await client.get_sub_agent("code-reviewer")
    await client.get_sub_agent_by_id("01XYZ")
    assert captured[0].url.path == "/api/v1/sub-agents/code-reviewer"
    assert captured[1].url.path == "/api/v1/sub-agents/by-id/01XYZ"
    await client.close()


async def test_get_sub_agent_assembled_returns_prompt_string():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/sub-agents/code-reviewer/assembled"
        return _envelope({"prompt": "You are a careful code reviewer."})

    client = _make_client(handler)
    out = await client.get_sub_agent_assembled("code-reviewer")
    assert out == "You are a careful code reviewer."
    await client.close()


async def test_get_sub_agent_assembled_handles_missing_prompt_field():
    """If the server returns something unexpected, return None rather than
    crashing the caller mid-task."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _envelope({"something_else": "..."})

    client = _make_client(handler)
    out = await client.get_sub_agent_assembled("x")
    assert out is None
    await client.close()


# ══ Service proxy ══════════════════════════════════════════════════════════


async def test_discover_services_default_prefix():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([{"id": "service-1"}])

    client = _make_client(handler)
    await client.discover_services()
    assert captured[0].url.path == "/api/v1/services"
    assert captured[0].url.params["capability_prefix"] == "data:"
    await client.close()


async def test_discover_services_custom_prefix():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.discover_services(capability_prefix="ops:")
    assert captured[0].url.params["capability_prefix"] == "ops:"
    await client.close()


async def test_service_request_forwards_method_and_path():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler)
    resp = await client.service_request("GET", "github", "repos/x/y")
    assert resp.json() == {"ok": True}
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/api/v1/proxy/github/repos/x/y"
    await client.close()


async def test_service_request_handles_leading_slash_in_path():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    client = _make_client(handler)
    await client.service_request("GET", "github", "/repos/x/y")
    # leading slash must be stripped, not produce //
    assert captured[0].url.path == "/api/v1/proxy/github/repos/x/y"
    await client.close()


async def test_service_request_no_path():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    client = _make_client(handler)
    await client.service_request("GET", "github")
    assert captured[0].url.path == "/api/v1/proxy/github"
    await client.close()


async def test_service_request_passes_json_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201)

    client = _make_client(handler)
    await client.service_request(
        "POST", "slack", "chat.postMessage",
        json={"channel": "#deploy", "text": "shipped"},
    )
    assert httpx_json(captured[0]) == {"channel": "#deploy", "text": "shipped"}
    await client.close()


async def test_service_request_merges_caller_headers_with_auth():
    """Regression for the bug where service_request passed headers= AND
    _request passed headers=self._headers() — Python raises
    `TypeError: got multiple values for keyword argument 'headers'`
    before the request leaves the client.  Verify the merged request has
    both the auth header and the caller-supplied one.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    client = _make_client(handler)
    await client.service_request(
        "GET", "github", "repos/x/y",
        headers={"Accept": "application/vnd.github.v3+json", "X-Trace": "abc"},
    )

    req = captured[0]
    # auth header is still there
    assert req.headers["authorization"] == "Bearer tok"
    # caller-supplied headers are merged in
    assert req.headers["accept"] == "application/vnd.github.v3+json"
    assert req.headers["x-trace"] == "abc"
    await client.close()


async def test_service_request_returns_raw_response_for_non_json():
    """Some proxied APIs return non-JSON (binary, XML). We return the raw
    Response so callers can pick `.text`, `.content`, or `.json()`."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"})

    client = _make_client(handler)
    resp = await client.service_request("GET", "s3", "objects/foo.png")
    assert resp.content.startswith(b"\x89PNG")
    assert resp.headers["content-type"] == "image/png"
    await client.close()


# ══ Drain mode ═════════════════════════════════════════════════════════════


async def test_enter_drain_default_no_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"status": "draining"})

    client = _make_client(handler)
    await client.enter_drain()
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/agents/drain"
    # No body provided when no fields set
    assert not req.content
    await client.close()


async def test_enter_drain_with_reason_and_deadline():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"status": "draining"})

    client = _make_client(handler)
    await client.enter_drain(reason="deploy", deadline_minutes=10)
    body = httpx_json(captured[0])
    assert body == {"reason": "deploy", "deadline_minutes": 10}
    await client.close()


async def test_exit_and_status_drain():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"status": "active"})

    client = _make_client(handler)
    await client.exit_drain()
    await client.drain_status()
    assert captured[0].method == "POST"
    assert captured[0].url.path == "/api/v1/agents/undrain"
    assert captured[1].method == "GET"
    assert captured[1].url.path == "/api/v1/agents/drain"
    await client.close()


# ══ Task tracing / replay ══════════════════════════════════════════════════


async def test_get_task():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/tasks/T1"
        return _raw({"id": "T1", "status": "completed"})

    client = _make_client(handler)
    out = await client.get_task("T1")
    assert out["status"] == "completed"
    await client.close()


async def test_get_task_trace():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/tasks/T1/trace"
        return _raw({"events": []})

    client = _make_client(handler)
    out = await client.get_task_trace("T1")
    assert out == {"events": []}
    await client.close()


async def test_replay_task_no_override():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"id": "T2"})

    client = _make_client(handler)
    await client.replay_task("T1")
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/tasks/T1/replay"
    # No body when no override
    assert not req.content
    await client.close()


async def test_replay_task_with_override():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"id": "T2"})

    client = _make_client(handler)
    await client.replay_task("T1", override_payload={"retry_at": "later"})
    body = httpx_json(captured[0])
    assert body == {"override_payload": {"retry_at": "later"}}
    await client.close()


async def test_compare_tasks_passes_query_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"diff": []})

    client = _make_client(handler)
    await client.compare_tasks("T1", "T2")
    req = captured[0]
    assert req.url.path == "/api/v1/hives/hive-x/tasks/compare"
    assert req.url.params["task_a"] == "T1"
    assert req.url.params["task_b"] == "T2"
    await client.close()


# ══ Dead-letter ════════════════════════════════════════════════════════════


async def test_list_dead_letter():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/tasks/dead-letter"
        return _envelope([{"id": "T1", "reason": "max_retries"}])

    client = _make_client(handler)
    out = await client.list_dead_letter()
    assert out == [{"id": "T1", "reason": "max_retries"}]
    await client.close()


async def test_get_dead_letter():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/tasks/T1/dead-letter"
        return _raw({"id": "T1", "reason": "max_retries", "trace": []})

    client = _make_client(handler)
    out = await client.get_dead_letter("T1")
    assert out["reason"] == "max_retries"
    await client.close()


# ══ Threads ════════════════════════════════════════════════════════════════


async def test_create_thread_no_args_sends_no_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "THR1"})

    client = _make_client(handler)
    await client.create_thread()
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/threads"
    assert not req.content
    await client.close()


async def test_create_thread_with_seed():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "THR1"})

    client = _make_client(handler)
    await client.create_thread(
        title="Auth incident", message="found a token leak", metadata={"sev": 2},
    )
    body = httpx_json(captured[0])
    assert body == {
        "title": "Auth incident",
        "message": "found a token leak",
        "metadata": {"sev": 2},
    }
    await client.close()


async def test_list_threads_passes_limit():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_threads(limit=10)
    assert captured[0].url.params["limit"] == "10"
    await client.close()


async def test_get_thread():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/threads/THR1"
        return _envelope({"id": "THR1", "messages": []})

    client = _make_client(handler)
    out = await client.get_thread("THR1")
    assert out["id"] == "THR1"
    await client.close()


async def test_append_thread_message_required_and_optional():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "M1"})

    client = _make_client(handler)
    await client.append_thread_message(
        "THR1", "hello world", task_id="T5", metadata={"role": "assistant"},
    )
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/threads/THR1/messages"
    assert httpx_json(req) == {
        "message": "hello world",
        "task_id": "T5",
        "metadata": {"role": "assistant"},
    }
    await client.close()


async def test_clear_and_delete_thread():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"cleared": 5})

    client = _make_client(handler)
    await client.clear_thread_messages("THR1")
    assert captured[-1].method == "DELETE"
    assert captured[-1].url.path == "/api/v1/hives/hive-x/threads/THR1/messages"

    def handler2(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _empty_204()

    client2 = _make_client(handler2)
    result = await client2.delete_thread("THR1")
    assert result is None
    assert captured[-1].url.path == "/api/v1/hives/hive-x/threads/THR1"
    assert captured[-1].method == "DELETE"
    await client.close()
    await client2.close()


# ══ Schedule pause / resume ════════════════════════════════════════════════


async def test_pause_and_resume_schedule():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"id": "SCH1", "status": "paused"})

    client = _make_client(handler)
    await client.pause_schedule("SCH1")
    assert captured[-1].method == "PATCH"
    assert captured[-1].url.path == "/api/v1/hives/hive-x/schedules/SCH1/pause"

    await client.resume_schedule("SCH1")
    assert captured[-1].method == "PATCH"
    assert captured[-1].url.path == "/api/v1/hives/hive-x/schedules/SCH1/resume"
    await client.close()


# ══ Events (publish) ═══════════════════════════════════════════════════════


async def test_publish_event_required_type_only():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"id": "EVT1"})

    client = _make_client(handler)
    await client.publish_event("deploy.finished")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/events"
    assert httpx_json(req) == {"type": "deploy.finished"}
    await client.close()


async def test_publish_event_with_payload():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _raw({"id": "EVT1"})

    client = _make_client(handler)
    await client.publish_event(
        "deploy.finished", payload={"env": "staging", "version": "1.2.3"},
    )
    body = httpx_json(captured[0])
    assert body == {
        "type": "deploy.finished",
        "payload": {"env": "staging", "version": "1.2.3"},
    }
    await client.close()


# ══ Helpers ════════════════════════════════════════════════════════════════


def httpx_json(request: httpx.Request) -> dict:
    """Read a JSON body off an httpx.Request (mock-mode bodies are bytes)."""
    import json
    return json.loads(request.content.decode())
