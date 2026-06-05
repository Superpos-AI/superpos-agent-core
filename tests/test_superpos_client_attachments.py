"""Tests for the attachment methods on SuperposClient.

Uses ``httpx.MockTransport`` to capture outbound requests without hitting
a real network.  Each test asserts the URL / multipart body / params the
client sends and the unwrapping it applies to the envelope.  Mirrors the
style of ``test_superpos_client_issues.py``.
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


def _envelope(data, meta=None, status=200):
    body = {"data": data}
    if meta is not None:
        body["meta"] = meta
    return httpx.Response(status, json=body)


# ── upload_attachment ───────────────────────────────────────────────────


async def test_upload_attachment_sends_multipart_with_issue_id(tmp_path):
    f = tmp_path / "repro.png"
    f.write_bytes(b"\x89PNG fake bytes")

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "att-1", "issue_id": "i1", "filename": "repro.png"}, status=201)

    client = _make_client(handler)
    result = await client.upload_attachment(
        file_path=str(f), issue_id="i1", description="a repro",
    )

    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/hives/hive-x/attachments"
    # multipart body carries the file part and the form fields
    content = req.content
    assert b'name="file"' in content
    assert b"repro.png" in content
    assert b'name="issue_id"' in content
    assert b"i1" in content
    assert b'name="description"' in content
    assert b"a repro" in content
    # unwraps the envelope's data
    assert result == {"id": "att-1", "issue_id": "i1", "filename": "repro.png"}
    await client.close()


async def test_upload_attachment_omits_unset_optional_fields(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("trace")

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope({"id": "att-2"}, status=201)

    client = _make_client(handler)
    await client.upload_attachment(file_path=str(f))

    content = captured[0].content
    assert b'name="file"' in content
    # No issue_id / task_id / description parts when not provided
    assert b'name="issue_id"' not in content
    assert b'name="task_id"' not in content
    assert b'name="description"' not in content
    await client.close()


# ── list_attachments ────────────────────────────────────────────────────


async def test_list_attachments_filters_by_issue_id():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope(
            [{"id": "att-1", "issue_id": "i1"}],
            meta={"total": 1, "current_page": 1},
        )

    client = _make_client(handler)
    result = await client.list_attachments(issue_id="i1", per_page=50)

    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/hives/hive-x/attachments"
    assert req.url.params["issue_id"] == "i1"
    assert req.url.params["per_page"] == "50"
    # full envelope returned (meta needed for pagination)
    assert result["data"][0]["id"] == "att-1"
    assert result["meta"]["total"] == 1
    await client.close()


async def test_list_attachments_forwards_page():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope(
            [{"id": "att-3", "issue_id": "i1"}],
            meta={"total": 120, "current_page": 2, "last_page": 3},
        )

    client = _make_client(handler)
    result = await client.list_attachments(issue_id="i1", page=2, per_page=50)

    req = captured[0]
    assert req.url.params["page"] == "2"
    assert req.url.params["per_page"] == "50"
    assert result["meta"]["current_page"] == 2
    await client.close()


async def test_list_attachments_no_filters_sends_no_params():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _envelope([])

    client = _make_client(handler)
    await client.list_attachments()

    assert dict(captured[0].url.params) == {}
    await client.close()


# ── delete_attachment ───────────────────────────────────────────────────


async def test_delete_attachment_sends_delete():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    client = _make_client(handler)
    await client.delete_attachment("att-9")

    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.path == "/api/v1/hives/hive-x/attachments/att-9"
    await client.close()
