"""Tests for the typed KnowledgeClient (TASK-298 / Phase A3 SDK).

Uses ``httpx.MockTransport`` to capture outbound requests without
hitting a real network — the same pattern as
``test_superpos_client_knowledge.py``. Each test asserts the URL/verb
and params/body the client sends, the envelope unwrapping it applies,
and the error behaviour (404 → KnowledgeNotFound for get_source, other
non-2xx → httpx.HTTPStatusError).
"""

from __future__ import annotations

import json

import httpx
import pytest

from superpos_agent_core import (
    BaseConfig,
    KnowledgeClient,
    KnowledgeNotFound,
    SuperposClient,
)

HIVE = "hive-x"
BASE = f"/api/v1/hives/{HIVE}/knowledge"


def _make_client(handler):
    """Build a KnowledgeClient wrapping a SuperposClient on a mock transport."""
    config = BaseConfig(
        superpos_base_url="https://test.example",
        superpos_hive_id=HIVE,
        superpos_agent_id="agent-x",
        superpos_api_token="tok",
    )
    sc = SuperposClient(config)
    sc._client = httpx.AsyncClient(
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),
    )
    return KnowledgeClient(sc)


def _envelope(data, meta=None, status=200):
    body = {"data": data, "meta": meta or {}, "errors": []}
    return httpx.Response(status, json=body)


def _capturing(response):
    """Return (handler, captured) — handler records every request."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    return handler, captured


# ── create_page ───────────────────────────────────────────────────────


async def test_create_page_posts_new_shape_and_unwraps():
    handler, captured = _capturing(_envelope({"id": "kxe_1", "type": "entity"}))
    client = _make_client(handler)

    result = await client.create_page(
        type="entity",
        slug="entity:redis-cluster-prod",
        body="# Redis\n\n[[source:01HX]]",
        frontmatter={"kind": "service"},
        source_ids=["01HX"],
        tags=["infra"],
    )

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == BASE
    sent = json.loads(req.content)
    assert sent["type"] == "entity"
    assert sent["slug"] == "entity:redis-cluster-prod"
    assert sent["body"].startswith("# Redis")
    assert sent["frontmatter"] == {"kind": "service"}
    assert sent["source_ids"] == ["01HX"]
    assert sent["tags"] == ["infra"]
    assert sent["scope"] == "hive"
    assert sent["visibility"] == "public"
    assert result == {"id": "kxe_1", "type": "entity"}


async def test_create_page_inline_sources_ingest_and_attach():
    handler, captured = _capturing(_envelope({"id": "kxe_2"}))
    client = _make_client(handler)

    await client.create_page(
        type="source_page",
        slug="source:abc",
        body="summary",
        sources=[{"kind": "url", "uri": "https://x", "content_sha256": "a" * 64}],
    )

    sent = json.loads(captured[0].content)
    assert sent["sources"] == [
        {"kind": "url", "uri": "https://x", "content_sha256": "a" * 64}
    ]
    # omitted optionals are not sent
    assert "source_ids" not in sent
    assert "tags" not in sent


# ── update_page ───────────────────────────────────────────────────────


async def test_update_page_puts_only_provided_fields():
    handler, captured = _capturing(_envelope({"id": "kxe_1", "version": 4}))
    client = _make_client(handler)

    result = await client.update_page("kxe_1", title="New Title")

    req = captured[0]
    assert req.method == "PUT"
    assert req.url.path == f"{BASE}/kxe_1"
    sent = json.loads(req.content)
    assert sent == {"title": "New Title"}
    assert result == {"id": "kxe_1", "version": 4}


async def test_update_page_body_and_frontmatter():
    handler, captured = _capturing(_envelope({"id": "kxe_1"}))
    client = _make_client(handler)

    await client.update_page("kxe_1", body="new body", frontmatter={"status": "ok"})

    sent = json.loads(captured[0].content)
    assert sent == {"body": "new body", "frontmatter": {"status": "ok"}}


# ── get_backlinks ─────────────────────────────────────────────────────


async def test_get_backlinks_hits_backlinks_endpoint():
    handler, captured = _capturing(_envelope([{"id": "kxe_9"}]))
    client = _make_client(handler)

    result = await client.get_backlinks("kxe_1")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"{BASE}/kxe_1/backlinks"
    assert result == [{"id": "kxe_9"}]


# ── list_by_type ──────────────────────────────────────────────────────


async def test_list_by_type_url_and_params():
    handler, captured = _capturing(_envelope([{"id": "kxe_1", "type": "topic"}]))
    client = _make_client(handler)

    result = await client.list_by_type("topic", limit=10, scope="organization")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"{BASE}/types/topic/list"
    assert req.url.params["limit"] == "10"
    assert req.url.params["scope"] == "organization"
    assert result == [{"id": "kxe_1", "type": "topic"}]


async def test_list_by_type_omits_scope_when_none():
    handler, captured = _capturing(_envelope([]))
    client = _make_client(handler)

    await client.list_by_type("entity")

    params = captured[0].url.params
    assert params["limit"] == "50"
    assert "scope" not in params


# ── synthesize_topic ──────────────────────────────────────────────────


async def test_synthesize_topic_posts_source_ids_and_slug():
    handler, captured = _capturing(
        _envelope({"task_id": "tsk_1", "status": "pending"})
    )
    client = _make_client(handler)

    result = await client.synthesize_topic(["01A", "01B"], slug="topic:x")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == f"{BASE}/synthesize-topic"
    sent = json.loads(req.content)
    assert sent == {"source_ids": ["01A", "01B"], "slug": "topic:x"}
    assert result == {"task_id": "tsk_1", "status": "pending"}


async def test_synthesize_topic_omits_slug_when_none():
    handler, captured = _capturing(_envelope({"task_id": "tsk_2"}))
    client = _make_client(handler)

    await client.synthesize_topic(["01A"])

    assert json.loads(captured[0].content) == {"source_ids": ["01A"]}


# ── ingest_source ─────────────────────────────────────────────────────


async def test_ingest_source_posts_required_and_optional_fields():
    handler, captured = _capturing(_envelope({"id": "src_1"}))
    client = _make_client(handler)

    result = await client.ingest_source(
        kind="url",
        uri="https://x",
        content_sha256="b" * 64,
        title="T",
        metadata={"a": 1},
        origin="hive",
    )

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == f"{BASE}/sources"
    sent = json.loads(req.content)
    assert sent == {
        "kind": "url",
        "uri": "https://x",
        "content_sha256": "b" * 64,
        "title": "T",
        "metadata": {"a": 1},
        "origin": "hive",
    }
    assert result == {"id": "src_1"}


# ── get_source ────────────────────────────────────────────────────────


async def test_get_source_hits_show_endpoint():
    handler, captured = _capturing(_envelope({"id": "src_1", "kind": "url"}))
    client = _make_client(handler)

    result = await client.get_source("src_1")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"{BASE}/sources/src_1"
    assert result == {"id": "src_1", "kind": "url"}


async def test_get_source_404_raises_knowledge_not_found():
    handler, _ = _capturing(
        httpx.Response(404, json={"data": None, "meta": {}, "errors": [
            {"message": "Source not found.", "code": "not_found"}
        ]})
    )
    client = _make_client(handler)

    with pytest.raises(KnowledgeNotFound) as exc_info:
        await client.get_source("missing")
    assert exc_info.value.source_id == "missing"


async def test_get_source_500_propagates_http_status_error():
    handler, _ = _capturing(httpx.Response(500, json={"errors": []}))
    client = _make_client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_source("src_1")


# ── list_sources ──────────────────────────────────────────────────────


async def test_list_sources_with_filters():
    handler, captured = _capturing(_envelope([{"id": "src_1"}]))
    client = _make_client(handler)

    result = await client.list_sources(kind="url", since="2026-01-01", limit=5)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == f"{BASE}/sources"
    assert req.url.params["kind"] == "url"
    assert req.url.params["since"] == "2026-01-01"
    assert req.url.params["limit"] == "5"
    assert result == [{"id": "src_1"}]


async def test_list_sources_omits_optional_filters():
    handler, captured = _capturing(_envelope([]))
    client = _make_client(handler)

    await client.list_sources()

    params = captured[0].url.params
    assert params["limit"] == "50"
    assert "kind" not in params
    assert "since" not in params


# ── hive override ─────────────────────────────────────────────────────


async def test_hive_override_changes_path():
    handler, captured = _capturing(_envelope([]))
    client = _make_client(handler)

    await client.list_by_type("entity", hive="other-hive")

    assert captured[0].url.path == "/api/v1/hives/other-hive/knowledge/types/entity/list"


# ── error propagation on writes ───────────────────────────────────────


async def test_create_page_422_propagates_http_status_error():
    handler, _ = _capturing(httpx.Response(422, json={"errors": []}))
    client = _make_client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await client.create_page(type="entity", slug="x", body="b")


# ── deferred methods ──────────────────────────────────────────────────


async def test_get_wiki_index_is_deferred():
    handler, captured = _capturing(_envelope({}))
    client = _make_client(handler)

    with pytest.raises(NotImplementedError, match="get_wiki_index is deferred"):
        await client.get_wiki_index()
    # must NOT have made a network call to a non-existent route
    assert captured == []


async def test_get_wiki_log_is_deferred():
    handler, captured = _capturing(_envelope({}))
    client = _make_client(handler)

    with pytest.raises(NotImplementedError, match="get_wiki_log is deferred"):
        await client.get_wiki_log()
    assert captured == []
