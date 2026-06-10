"""Tests for the knowledge read methods on SuperposClient.

Uses ``httpx.MockTransport`` to capture outbound requests without hitting
a real network.  Each test asserts both the URL/params the client sends
and the unwrapping it applies to the envelope.
"""

from __future__ import annotations

import json

import httpx
import pytest

from superpos_agent_core import BaseConfig, SuperposClient


def _make_client(handler):
    """Build a SuperposClient whose httpx.AsyncClient uses the given handler."""
    config = BaseConfig(
        superpos_base_url="https://test.example",
        superpos_hive_id="hive-x",
        superpos_agent_id="agent-x",
        superpos_api_token="tok",
    )
    client = SuperposClient(config)
    # Swap the live transport for a mock — keeps base_url/timeout/etc.
    client._client = httpx.AsyncClient(
        base_url="https://test.example",
        transport=httpx.MockTransport(handler),
    )
    return client


def _envelope(data, meta=None):
    body = {"data": data}
    if meta is not None:
        body["meta"] = meta
    return httpx.Response(200, json=body)


# ── search_knowledge ─────────────────────────────────────────────────────


async def test_search_knowledge_hits_search_endpoint_with_q():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope(
            [{"id": "k1", "key": "deploy.staging", "value": {"summary": "..."}}],
            meta={"total": 1, "query": "deploy"},
        )

    client = _make_client(handler)
    results = await client.search_knowledge("deploy")

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/knowledge/search"
    assert req.url.params["q"] == "deploy"
    assert req.url.params["limit"] == "50"
    # semantic flag absent on default call
    assert "semantic" not in req.url.params
    # unwrapped to the data list
    assert results == [{"id": "k1", "key": "deploy.staging", "value": {"summary": "..."}}]
    await client.close()


async def test_search_knowledge_semantic_flag_passed():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    with pytest.warns(DeprecationWarning, match="semantic=True"):
        await client.search_knowledge("auth migration", semantic=True, limit=10)

    req = captured[0]
    assert req.url.params["q"] == "auth migration"
    assert req.url.params["mode"] == "semantic"
    assert "semantic" not in req.url.params
    assert req.url.params["limit"] == "10"
    await client.close()


async def test_search_knowledge_mode_passed():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    for mode in ("fts", "semantic", "hybrid"):
        await client.search_knowledge("x", mode=mode)

    assert [r.url.params["mode"] for r in captured] == ["fts", "semantic", "hybrid"]
    await client.close()


async def test_search_knowledge_explain_passed():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.search_knowledge("x", explain=True)
    await client.search_knowledge("y")  # default False — should not appear

    assert captured[0].url.params["explain"] == "true"
    assert "explain" not in captured[1].url.params
    await client.close()


async def test_search_knowledge_mode_wins_over_semantic():
    """If both `mode` and the deprecated `semantic=True` are passed, `mode`
    wins — and the deprecation warning still fires."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    with pytest.warns(DeprecationWarning):
        await client.search_knowledge("x", mode="hybrid", semantic=True)

    assert captured[0].url.params["mode"] == "hybrid"
    await client.close()


async def test_search_knowledge_raises_when_both_q_and_scope_missing():
    """The server returns 400 if neither q nor scope is provided; we
    short-circuit with a ValueError so the caller mistake doesn't
    masquerade as a network failure."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _envelope([])

    client = _make_client(handler)
    with pytest.raises(ValueError, match="at least one of"):
        await client.search_knowledge()  # no q, no scope

    assert not called, "no HTTP request should be sent for an invalid call"
    await client.close()


async def test_search_knowledge_scope_only_no_q():
    """The server allows scope-only queries; client must not require q."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.search_knowledge(scope="apiary")

    req = captured[0]
    assert "q" not in req.url.params
    assert req.url.params["scope"] == "apiary"
    await client.close()


# ── list_knowledge ───────────────────────────────────────────────────────


async def test_list_knowledge_passes_all_filters():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([{"id": "k1"}])

    client = _make_client(handler)
    await client.list_knowledge(
        key="deploy.*",
        scope="hive",
        tags="prod,critical",
        stale_days=14,
        sort="least_read",
        limit=25,
    )

    req = captured[0]
    assert req.url.path == "/api/v1/hives/hive-x/knowledge"
    assert req.url.params["key"] == "deploy.*"
    assert req.url.params["scope"] == "hive"
    assert req.url.params["tags"] == "prod,critical"
    assert req.url.params["stale_days"] == "14"
    assert req.url.params["sort"] == "least_read"
    assert req.url.params["limit"] == "25"
    await client.close()


async def test_list_knowledge_omits_unset_filters():
    """None-valued filters must not appear in the query string at all —
    otherwise the server sees `key=None` which it'd interpret as a literal."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_knowledge()

    req = captured[0]
    for absent in ("key", "scope", "tags", "stale_days", "sort"):
        assert absent not in req.url.params, f"{absent} should not be sent"
    # limit always sent
    assert req.url.params["limit"] == "50"
    await client.close()


# ── get_knowledge ────────────────────────────────────────────────────────


async def test_get_knowledge_returns_entry_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/knowledge/01ABC"
        return _envelope({"id": "01ABC", "key": "deploy.staging", "value": {"x": 1}})

    client = _make_client(handler)
    entry = await client.get_knowledge("01ABC")
    assert entry["id"] == "01ABC"
    assert entry["value"] == {"x": 1}
    await client.close()


# ── get_knowledge_graph ──────────────────────────────────────────────────


async def test_get_knowledge_graph_default_depth_and_max_nodes():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"nodes": [], "edges": []})

    client = _make_client(handler)
    await client.get_knowledge_graph("01ABC")

    req = captured[0]
    assert req.url.path == "/api/v1/hives/hive-x/knowledge/01ABC/graph"
    assert req.url.params["depth"] == "2"
    assert req.url.params["max_nodes"] == "50"
    assert "link_types" not in req.url.params
    await client.close()


async def test_get_knowledge_graph_custom_args():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"nodes": [], "edges": []})

    client = _make_client(handler)
    await client.get_knowledge_graph(
        "01ABC", depth=4, max_nodes=100, link_types="decides,depends_on",
    )

    req = captured[0]
    assert req.url.params["depth"] == "4"
    assert req.url.params["max_nodes"] == "100"
    assert req.url.params["link_types"] == "decides,depends_on"
    await client.close()


# ── index endpoints ──────────────────────────────────────────────────────


async def test_knowledge_topics_hits_topics_index():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/knowledge/index/topics"
        return _envelope({"topics": [{"name": "auth", "count": 5}]})

    client = _make_client(handler)
    out = await client.knowledge_topics()
    assert out == {"topics": [{"name": "auth", "count": 5}]}
    await client.close()


async def test_knowledge_decisions_hits_decisions_index():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/hives/hive-x/knowledge/index/decisions"
        return _envelope({"decisions": []})

    client = _make_client(handler)
    out = await client.knowledge_decisions()
    assert out == {"decisions": []}
    await client.close()


# ── Auth header is sent on knowledge requests ────────────────────────────


async def test_auth_header_sent_on_knowledge_request():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.search_knowledge("anything")

    auth = captured[0].headers.get("authorization", "")
    assert auth.startswith("Bearer ")
    assert auth.endswith("tok")
    await client.close()


# ── Response-shape resilience ────────────────────────────────────────────


async def test_unwrap_handles_responses_without_data_envelope():
    """Some endpoints might return the list directly rather than wrapping
    in {data: [...]}.  The unwrap should pass through unchanged."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "raw"}])

    client = _make_client(handler)
    out = await client.search_knowledge("x")
    assert out == [{"id": "raw"}]
    await client.close()


# ── Typed-page list_by_type / backlinks URL shapes ──────────────────────
#
# AG-2 originally wired these to the wrong URLs (verified by probing the
# live server — see PR #35).  These tests pin the correct shapes:
#   list-by-type → GET /knowledge/types/{type}/list  (NOT /knowledge?type=)
#   backlinks    → GET /knowledge/{entry}/backlinks  (NOT /knowledge/backlinks/{slug})
# The /knowledge?type= query-param is silently ignored on the index endpoint.


async def test_list_knowledge_by_type_hits_types_list_endpoint():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([{"id": "k1", "type": "topic"}], meta={"total": 1, "type": "topic"})

    client = _make_client(handler)
    out = await client.list_knowledge_by_type("topic", limit=10)

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    # Correct URL: literal /types/{type}/list, NOT /knowledge?type=topic
    assert req.url.path == "/api/v1/hives/hive-x/knowledge/types/topic/list"
    # And the type is in the path, not the query string
    assert "type" not in req.url.params
    assert req.url.params["limit"] == "10"
    assert out == [{"id": "k1", "type": "topic"}]
    await client.close()


async def test_list_knowledge_backlinks_hits_entry_backlinks_endpoint():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([{"slug": "proposal-x", "id": "01OTHER"}], meta={"total": 1})

    client = _make_client(handler)
    out = await client.list_knowledge_backlinks("01TARGET")

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    # Correct URL: per-entry sub-resource, NOT /knowledge/backlinks/{slug}
    assert req.url.path == "/api/v1/hives/hive-x/knowledge/01TARGET/backlinks"
    assert out == [{"slug": "proposal-x", "id": "01OTHER"}]
    await client.close()


# ── get_knowledge_by_slug — search → get two-hop ─────────────────────────
#
# There is no GET /knowledge/slug/{slug} route on the server, so the client
# resolves the slug via the search endpoint, then fetches the full entry by
# its ULID.  These tests pin that two-hop and assert it NEVER requests the
# non-existent /knowledge/slug/... path.


async def test_get_knowledge_by_slug_searches_then_gets_by_id():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/knowledge/search"):
            # Return two candidates; the exact-slug match is NOT first to
            # prove the selection prefers an exact slug over result order.
            return _envelope(
                [
                    {"id": "01NEAR", "slug": "proposal-knowledge-wiki-v2"},
                    {"id": "01EXACT", "slug": "proposal-knowledge-wiki"},
                ],
                meta={"total": 2},
            )
        return _envelope(
            {"id": "01EXACT", "slug": "proposal-knowledge-wiki", "value": {"x": 1}},
        )

    client = _make_client(handler)
    out = await client.get_knowledge_by_slug("proposal-knowledge-wiki")

    # Two hops: search first, then get-by-id — and never the slug route.
    assert len(captured) == 2
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/api/v1/hives/hive-x/knowledge/search"
    assert captured[0].url.params["q"] == "proposal-knowledge-wiki"
    # Exact-slug match wins: the second hop fetches 01EXACT, not 01NEAR.
    assert captured[1].method == "GET"
    assert captured[1].url.path == "/api/v1/hives/hive-x/knowledge/01EXACT"
    # The non-existent slug route is never requested.
    assert all("/knowledge/slug/" not in req.url.path for req in captured)
    assert out == {"id": "01EXACT", "slug": "proposal-knowledge-wiki", "value": {"x": 1}}
    await client.close()


async def test_get_knowledge_by_slug_falls_back_to_first_result():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/knowledge/search"):
            # No exact slug match — fall back to the first result.
            return _envelope([{"id": "01FIRST", "slug": "other-page"}])
        return _envelope({"id": "01FIRST", "slug": "other-page"})

    client = _make_client(handler)
    out = await client.get_knowledge_by_slug("missing-exact")

    assert captured[1].url.path == "/api/v1/hives/hive-x/knowledge/01FIRST"
    assert out == {"id": "01FIRST", "slug": "other-page"}
    await client.close()


async def test_get_knowledge_by_slug_raises_when_no_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return _envelope([])

    client = _make_client(handler)
    with pytest.raises(ValueError, match="no knowledge entry found for slug"):
        await client.get_knowledge_by_slug("nope")
    await client.close()
