"""Tests for the track methods on SuperposClient.

Uses ``httpx.MockTransport`` to capture outbound requests without hitting
a real network.  Each test asserts both the URL/body the client sends
and the unwrapping it applies to the envelope.  Mirrors the style of
``test_superpos_client_knowledge.py`` and ``test_superpos_client_issues.py``.
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


# ── list_tracks ──────────────────────────────────────────────────────────


async def test_list_tracks_hits_tracks_index():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([
            {"id": "t1", "slug": "k1", "name": "Knowledge Wiki", "state": "active"},
        ])

    client = _make_client(handler)
    tracks = await client.list_tracks()

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/tracks"
    # No filters → no params (None-valued filter is omitted, not sent as ?status=)
    assert dict(req.url.params) == {}
    # Index returns the list, spec is omitted
    assert tracks == [{"id": "t1", "slug": "k1", "name": "Knowledge Wiki", "state": "active"}]
    await client.close()


async def test_list_tracks_passes_status_and_tag_filters():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_tracks(status="active", tag="infra")

    req = captured[0]
    assert req.url.params["status"] == "active"
    assert req.url.params["tag"] == "infra"
    await client.close()


async def test_list_tracks_omits_unset_filters():
    """None-valued filters must not appear in the query string at all —
    otherwise the server sees ?status=None which it would not interpret."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _envelope([])

    client = _make_client(handler)
    await client.list_tracks()

    # Verified by the previous test; this one ensures the contract holds
    # when only one filter is set.
    await client.list_tracks(status="active")
    await client.list_tracks(tag="infra")
    await client.close()


# ── get_track_by_slug ────────────────────────────────────────────────────


async def test_get_track_by_slug_returns_full_payload_with_spec():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({
            "id": "t1",
            "slug": "k1",
            "name": "Knowledge Wiki",
            "state": "active",
            "spec": "## Status\n\nActive.",
        })

    client = _make_client(handler)
    track = await client.get_track_by_slug("k1")

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/tracks/k1"
    # spec is included in the get response (unlike index)
    assert track["spec"] == "## Status\n\nActive."
    assert track["slug"] == "k1"
    await client.close()


# ── create_track ─────────────────────────────────────────────────────────


async def test_create_track_minimal_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "t1", "slug": "k1", "name": "Knowledge Wiki", "state": "planning"},
                         status=201)

    client = _make_client(handler)
    track = await client.create_track(slug="k1", name="Knowledge Wiki")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/tracks"
    body = json.loads(req.content)
    assert body == {"slug": "k1", "name": "Knowledge Wiki"}
    assert track["slug"] == "k1"
    await client.close()


async def test_create_track_full_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "t1", "state": "active"}, status=201)

    client = _make_client(handler)
    await client.create_track(
        slug="k1",
        name="Knowledge Wiki",
        description="Karpathy-style typed pages",
        spec="## Status\n\nActive.",
        state="active",
    )

    req = captured[0]
    body = json.loads(req.content)
    assert body == {
        "slug": "k1",
        "name": "Knowledge Wiki",
        "description": "Karpathy-style typed pages",
        "spec": "## Status\n\nActive.",
        "state": "active",
    }
    await client.close()


async def test_create_track_omits_unset_optional_fields():
    """description/spec/state default to None — they must not be sent as
    JSON null, otherwise the server validator (which expects absence or
    a real string) might 422."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "description" not in body
        assert "spec" not in body
        assert "state" not in body
        return _envelope({"id": "t1"}, status=201)

    client = _make_client(handler)
    await client.create_track(slug="k1", name="Knowledge Wiki")
    await client.close()


# ── patch_track ──────────────────────────────────────────────────────────


async def test_patch_track_with_all_fields():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "t1", "name": "New name"})

    client = _make_client(handler)
    await client.patch_track(
        "k1",
        name="New name",
        description="New desc",
        spec="## New spec",
    )

    req = captured[0]
    assert req.method == "PATCH"
    assert req.url.path == "/api/v1/hives/hive-x/tracks/k1"
    body = json.loads(req.content)
    assert body == {
        "name": "New name",
        "description": "New desc",
        "spec": "## New spec",
    }
    await client.close()


async def test_patch_track_partial_fields():
    """Only set fields are sent — server interprets missing keys as 'no change'."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"name": "Renamed"}
        assert "description" not in body
        assert "spec" not in body
        return _envelope({"id": "t1", "name": "Renamed"})

    client = _make_client(handler)
    await client.patch_track("k1", name="Renamed")
    await client.close()


# ── link_track_issue ─────────────────────────────────────────────────────


async def test_link_track_issue_posts_with_issue_id_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"track_id": "t1", "issue_id": "i1"})

    client = _make_client(handler)
    out = await client.link_track_issue("k1", "i1")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/tracks/k1/issues"
    body = json.loads(req.content)
    assert body == {"issue_id": "i1"}
    assert out == {"track_id": "t1", "issue_id": "i1"}
    await client.close()


# ── unlink_track_issue ───────────────────────────────────────────────────


async def test_unlink_track_issue_sends_delete():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    client = _make_client(handler)
    result = await client.unlink_track_issue("k1", "i1")

    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/hives/hive-x/tracks/k1/issues/i1"
    # 204 No Content → method returns None
    assert result is None
    await client.close()


# ── Auth header is sent on track requests ────────────────────────────────


async def test_auth_header_sent_on_track_request():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_tracks()

    auth = captured[0].headers.get("authorization", "")
    assert auth.startswith("Bearer ")
    assert auth.endswith("tok")
    await client.close()
