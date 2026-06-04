"""Tests for the GitHub methods on SuperposClient.

Uses ``httpx.MockTransport`` to capture outbound requests without a network,
mirroring ``test_superpos_client_issues.py``.
"""

from __future__ import annotations

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


# ── list_github_connections ────────────────────────────────────────────


async def test_list_github_connections_filters_and_unwraps():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": [
                {"id": "c1", "name": "github-bot",
                 "metadata": {"auth_type": "github_app"}},
            ]},
        )

    client = _make_client(handler)
    result = await client.list_github_connections()

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/services"
    assert dict(req.url.params) == {
        "type": "github",
        "status": "active",
        "page": "1",
        "per_page": "100",
    }
    assert result[0]["name"] == "github-bot"
    # A short first page (no meta) must not trigger a second request.
    assert len(captured) == 1
    await client.close()


async def test_list_github_connections_paginates_until_last_page():
    # The GitHub App connection lives on page 2, behind a full first page.
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        page = int(dict(request.url.params).get("page", "1"))
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "noise", "name": "other"}],
                    "meta": {"current_page": 1, "last_page": 2, "has_more": True},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "c2", "name": "github-app-pg2",
                     "metadata": {"auth_type": "github_app"}},
                ],
                "meta": {"current_page": 2, "last_page": 2, "has_more": False},
            },
        )

    client = _make_client(handler)
    result = await client.list_github_connections()

    assert [dict(r.url.params).get("page") for r in captured] == ["1", "2"]
    assert {c["name"] for c in result} == {"other", "github-app-pg2"}
    await client.close()


async def test_list_github_connections_returns_empty_on_forbidden():
    def handler(request: httpx.Request) -> httpx.Response:
        # No services.read permission → must not raise; callers fall back to
        # the static GITHUB_TOKEN path.
        return httpx.Response(403, json={"message": "forbidden"})

    client = _make_client(handler)
    assert await client.list_github_connections() == []
    await client.close()


# ── mint_github_token ──────────────────────────────────────────────────


async def test_mint_github_token_posts_and_unwraps():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": {"token": "ghs_minted", "expires_at": "2030-01-01T00:00:00Z"}},
        )

    client = _make_client(handler)
    result = await client.mint_github_token("c1")

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/github/installation-token"
    import json as _json
    body = _json.loads(req.content)
    # The broker mints an installation-wide token; the request carries only the
    # connection id (no repo scope, which the broker does not honour).
    assert body == {"service_connection_id": "c1"}
    assert result["token"] == "ghs_minted"
    await client.close()
