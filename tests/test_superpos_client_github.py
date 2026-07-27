"""Tests for the GitHub methods on SuperposClient.

Uses ``httpx.MockTransport`` to capture outbound requests without a network,
mirroring ``test_superpos_client_issues.py``.
"""

from __future__ import annotations

import httpx

from superpos_agent_core import BaseConfig, GitHubDiscoveryForbidden, SuperposClient


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


async def test_list_github_connections_raises_on_forbidden():
    def handler(request: httpx.Request) -> httpx.Response:
        # No services.read permission → must raise a typed exception so
        # callers can distinguish "denied" from "no connection exists".
        return httpx.Response(403, json={"message": "forbidden"})

    client = _make_client(handler)
    try:
        await client.list_github_connections()
    except GitHubDiscoveryForbidden as exc:
        assert exc.status_code == 403
        assert "services.read" in str(exc)
        # The original HTTPStatusError must be preserved as __cause__ for
        # traceback-style debugging.
        assert isinstance(exc.__cause__, httpx.HTTPStatusError)
    else:  # pragma: no cover
        raise AssertionError("expected GitHubDiscoveryForbidden")
    await client.close()


async def test_list_github_connections_raises_on_unauthorized():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthenticated"})

    client = _make_client(handler)
    try:
        await client.list_github_connections()
    except GitHubDiscoveryForbidden as exc:
        assert exc.status_code == 401
    else:  # pragma: no cover
        raise AssertionError("expected GitHubDiscoveryForbidden")
    await client.close()


async def test_list_github_connections_propagates_other_http_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        # 500s and other 4xx must propagate as httpx errors, not be
        # silently swallowed.
        return httpx.Response(500, json={"message": "boom"})

    client = _make_client(handler)
    try:
        await client.list_github_connections()
    except GitHubDiscoveryForbidden:  # pragma: no cover
        raise AssertionError("500 must not be turned into a discovery error")
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 500
    else:  # pragma: no cover
        raise AssertionError("expected httpx.HTTPStatusError")
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


# ── resolve_mcp_credentials ────────────────────────────────────────────


async def test_resolve_mcp_credentials_posts_and_unwraps():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": {
                "service_connection_id": "c1",
                "credentials": {"API_KEY": "sk-secret"},
                "missing": [],
                "expires_at": None,
            }},
        )

    client = _make_client(handler)
    result = await client.resolve_mcp_credentials("c1", ["API_KEY"])

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/mcp/credentials"
    import json as _json
    body = _json.loads(req.content)
    # Only the connection id and the bare env NAMES cross the wire — never
    # ``KEY=value`` forms and never resolved values.
    assert body == {"service_connection_id": "c1", "keys": ["API_KEY"]}
    # The ``data`` envelope is unwrapped for the caller.
    assert result["credentials"] == {"API_KEY": "sk-secret"}
    assert result["missing"] == []
    await client.close()


# ── get_persona ────────────────────────────────────────────────────────


async def test_get_persona_hits_persona_endpoint_and_unwraps():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": {"name": "p", "github": {"default_connection_id": "d1"}}},
        )

    client = _make_client(handler)
    result = await client.get_persona()

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/persona"
    assert result["github"]["default_connection_id"] == "d1"
    await client.close()


async def test_get_persona_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"message": "no persona"}]})

    client = _make_client(handler)
    assert await client.get_persona() is None
    await client.close()


# ── resolve_github_connection_id ───────────────────────────────────────


async def test_resolve_github_connection_id_prefers_default():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"github": {
                "default_connection_id": "default-uuid",
                "connections": [
                    {"service_connection_id": "default-uuid", "name": "a",
                     "broker_compatible": True},
                    {"service_connection_id": "other-uuid", "name": "b",
                     "broker_compatible": True},
                ],
            }}},
        )

    client = _make_client(handler)
    assert await client.resolve_github_connection_id() == "default-uuid"
    await client.close()


async def test_resolve_github_connection_id_single_connection_no_default():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"github": {
                "default_connection_id": None,
                "connections": [
                    {"service_connection_id": "only-uuid", "name": "a",
                     "broker_compatible": True},
                ],
            }}},
        )

    client = _make_client(handler)
    assert await client.resolve_github_connection_id() == "only-uuid"
    await client.close()


async def test_resolve_github_connection_id_pat_only_returns_none():
    # A PAT-only persona has exactly one connection that is NOT
    # broker-compatible and no default_connection_id.  The broker cannot mint
    # an installation token from a PAT, so the resolver must NOT present that
    # PAT id as the (default) connection — it returns None so callers fall
    # through to the static GITHUB_TOKEN path.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"github": {
                "default_connection_id": None,
                "connections": [
                    {"service_connection_id": "pat-uuid", "name": "pat",
                     "broker_compatible": False},
                ],
            }}},
        )

    client = _make_client(handler)
    assert await client.resolve_github_connection_id() is None
    await client.close()


async def test_resolve_github_connection_id_default_not_present_returns_none():
    # A stale default pointing at a connection id not in the block must not be
    # returned; here there is also no single broker-compatible fallback.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"github": {
                "default_connection_id": "ghost-uuid",
                "connections": [
                    {"service_connection_id": "a-uuid", "name": "a",
                     "broker_compatible": True},
                    {"service_connection_id": "b-uuid", "name": "b",
                     "broker_compatible": True},
                ],
            }}},
        )

    client = _make_client(handler)
    assert await client.resolve_github_connection_id() is None
    await client.close()


async def test_resolve_github_connection_id_pat_default_returns_none():
    # A default naming a PAT (broker_compatible=False) connection must not be
    # returned even though the id is present in the connections list.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"github": {
                "default_connection_id": "pat-uuid",
                "connections": [
                    {"service_connection_id": "pat-uuid", "name": "pat",
                     "broker_compatible": False},
                    {"service_connection_id": "app-uuid", "name": "app",
                     "broker_compatible": True},
                ],
            }}},
        )

    client = _make_client(handler)
    # Falls back to the sole broker-compatible connection, never the PAT.
    assert await client.resolve_github_connection_id() == "app-uuid"
    await client.close()


async def test_resolve_github_connection_id_broker_default_kept():
    # An explicit default that IS broker-compatible is honoured even when a
    # PAT connection is also present.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"github": {
                "default_connection_id": "app-uuid",
                "connections": [
                    {"service_connection_id": "pat-uuid", "name": "pat",
                     "broker_compatible": False},
                    {"service_connection_id": "app-uuid", "name": "app",
                     "broker_compatible": True},
                    {"service_connection_id": "app2-uuid", "name": "app2",
                     "broker_compatible": True},
                ],
            }}},
        )

    client = _make_client(handler)
    assert await client.resolve_github_connection_id() == "app-uuid"
    await client.close()


async def test_resolve_github_connection_id_ambiguous_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"github": {
                "default_connection_id": None,
                "connections": [
                    {"service_connection_id": "a-uuid", "name": "a"},
                    {"service_connection_id": "b-uuid", "name": "b"},
                ],
            }}},
        )

    client = _make_client(handler)
    assert await client.resolve_github_connection_id() is None
    await client.close()


async def test_resolve_github_connection_id_no_github_block_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"name": "p"}})

    client = _make_client(handler)
    assert await client.resolve_github_connection_id() is None
    await client.close()
