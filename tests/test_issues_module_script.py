"""Unit tests for the bundled superpos-issues CLI's attachment + discussion
subcommands.

The script ships without a ``.py`` extension (it's a PATH executable), so we
load it via importlib from the bundled modules dir and exercise its arg
parsing and request-dispatch logic with ``SuperposClient`` methods mocked —
no network.  Mirrors ``test_knowledge_module_script.py``.
"""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import httpx
import pytest

from superpos_agent_core import bundled_modules_dir

_SCRIPT = (
    Path(bundled_modules_dir())
    / "superpos-issues" / "scripts" / "superpos-issues"
)


def _load_script():
    loader = SourceFileLoader("_superpos_issues_cli", str(_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _set_env(monkeypatch):
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")


def test_script_exists_and_is_executable():
    assert _SCRIPT.is_file()
    assert _SCRIPT.stat().st_mode & 0o111  # at least one execute bit


# ── arg parsing ─────────────────────────────────────────────────────────


def test_parser_attach_requires_issue_and_file():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["attach", "--issue-id", "i1"])  # missing --file
    args = parser.parse_args(["attach", "--issue-id", "i1", "--file", "./x.png", "--description", "d"])
    assert args.cmd == "attach"
    assert args.issue_id == "i1"
    assert args.file == "./x.png"
    assert args.description == "d"


def test_parser_attachments_requires_issue_id():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["attachments"])
    args = parser.parse_args(["attachments", "--issue-id", "i1", "--per-page", "10"])
    assert args.cmd == "attachments" and args.issue_id == "i1" and args.per_page == 10
    assert args.page is None
    paged = parser.parse_args(["attachments", "--issue-id", "i1", "--page", "2", "--per-page", "10"])
    assert paged.page == 2 and paged.per_page == 10


def test_parser_detach_takes_positional_id():
    mod = _load_script()
    args = mod._build_parser().parse_args(["detach", "att-1"])
    assert args.cmd == "detach" and args.attachment_id == "att-1"


def test_parser_comment_and_discussion():
    mod = _load_script()
    parser = mod._build_parser()
    c = parser.parse_args(["comment", "--issue-id", "i1", "--message", "hi"])
    assert c.cmd == "comment" and c.issue_id == "i1" and c.message == "hi"
    d = parser.parse_args(["discussion", "--issue-id", "i1"])
    assert d.cmd == "discussion" and d.issue_id == "i1"


# ── dispatch: attachments ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_calls_upload_attachment(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)

    mock_upload = AsyncMock(return_value={"id": "att-1", "issue_id": "i1"})
    with patch.object(mod.SuperposClient, "upload_attachment", mock_upload), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(
            ["attach", "--issue-id", "i1", "--file", "./repro.png", "--description", "d"],
        )
        rc = await mod._run(args)

    assert rc == 0
    mock_upload.assert_awaited_once_with(file_path="./repro.png", issue_id="i1", description="d")


@pytest.mark.asyncio
async def test_attachments_calls_list_attachments(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)

    mock_list = AsyncMock(return_value={"data": [], "meta": {"total": 0}})
    with patch.object(mod.SuperposClient, "list_attachments", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["attachments", "--issue-id", "i1"])
        rc = await mod._run(args)

    assert rc == 0
    mock_list.assert_awaited_once_with(issue_id="i1", page=None, per_page=None)


@pytest.mark.asyncio
async def test_attachments_forwards_page(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)

    mock_list = AsyncMock(return_value={"data": [], "meta": {"current_page": 2}})
    with patch.object(mod.SuperposClient, "list_attachments", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(
            ["attachments", "--issue-id", "i1", "--page", "2", "--per-page", "25"],
        )
        rc = await mod._run(args)

    assert rc == 0
    mock_list.assert_awaited_once_with(issue_id="i1", page=2, per_page=25)


@pytest.mark.asyncio
async def test_detach_calls_delete_attachment(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)

    mock_delete = AsyncMock(return_value=None)
    with patch.object(mod.SuperposClient, "delete_attachment", mock_delete), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["detach", "att-7"])
        rc = await mod._run(args)

    assert rc == 0
    mock_delete.assert_awaited_once_with("att-7")


# ── dispatch: comment (auto-create-thread) ──────────────────────────────


@pytest.mark.asyncio
async def test_comment_creates_and_links_thread_when_none(monkeypatch):
    """First comment on an issue with no thread: create_thread → update_issue
    (link) → append_thread_message."""
    mod = _load_script()
    _set_env(monkeypatch)

    # discover no thread → link → post-link refetch (our link won) →
    # post-append verify (still ours).
    mock_get = AsyncMock(side_effect=[
        {"id": "i1", "title": "Bug", "thread_id": None},
        {"id": "i1", "title": "Bug", "thread_id": "th-1"},
        {"id": "i1", "title": "Bug", "thread_id": "th-1"},
    ])
    mock_create_thread = AsyncMock(return_value={"id": "th-1"})
    mock_update = AsyncMock(return_value={"id": "i1", "thread_id": "th-1"})
    mock_append = AsyncMock(return_value={"id": "msg-1", "message": "hi"})

    with patch.object(mod.SuperposClient, "get_issue", mock_get), \
         patch.object(mod.SuperposClient, "create_thread", mock_create_thread), \
         patch.object(mod.SuperposClient, "update_issue", mock_update), \
         patch.object(mod.SuperposClient, "append_thread_message", mock_append), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["comment", "--issue-id", "i1", "--message", "hi"])
        rc = await mod._run(args)

    assert rc == 0
    mock_get.assert_awaited_with("i1")
    mock_create_thread.assert_awaited_once()
    # thread linked back onto the issue
    mock_update.assert_awaited_once_with("i1", thread_id="th-1")
    # message appended to the newly created thread, exactly once (no overwrite)
    mock_append.assert_awaited_once_with("th-1", "hi")


@pytest.mark.asyncio
async def test_comment_reuses_winning_thread_on_concurrent_first_comment(monkeypatch):
    """Concurrent first comments: we create + link our own thread, but a
    racing caller's link won. The re-fetch must make us append into the
    issue's authoritative thread, not our orphaned local one — otherwise the
    message is lost from the issue's discussion."""
    mod = _load_script()
    _set_env(monkeypatch)

    # 1st get: no thread yet. 2nd get (post-link): a different thread already
    # won. 3rd get (post-append verify): still the winner — converged.
    mock_get = AsyncMock(side_effect=[
        {"id": "i1", "title": "Bug", "thread_id": None},
        {"id": "i1", "title": "Bug", "thread_id": "th-winner"},
        {"id": "i1", "title": "Bug", "thread_id": "th-winner"},
    ])
    mock_create_thread = AsyncMock(return_value={"id": "th-mine"})
    mock_update = AsyncMock(return_value={"id": "i1", "thread_id": "th-mine"})
    mock_append = AsyncMock(return_value={"id": "msg-9"})

    with patch.object(mod.SuperposClient, "get_issue", mock_get), \
         patch.object(mod.SuperposClient, "create_thread", mock_create_thread), \
         patch.object(mod.SuperposClient, "update_issue", mock_update), \
         patch.object(mod.SuperposClient, "append_thread_message", mock_append), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["comment", "--issue-id", "i1", "--message", "hi"])
        rc = await mod._run(args)

    assert rc == 0
    # appended into the winning thread, NOT our locally created th-mine
    mock_append.assert_awaited_once_with("th-winner", "hi")


@pytest.mark.asyncio
async def test_comment_defers_to_winner_on_link_conflict(monkeypatch):
    """Forward-compatible with a backend that atomically rejects replacing an
    already-claimed ``thread_id``: when ``update_issue`` raises 409/422, the
    loser must refetch and append into the issue's winning thread rather than
    surfacing the error or writing to its own orphaned thread."""
    mod = _load_script()
    _set_env(monkeypatch)

    conflict = httpx.HTTPStatusError(
        "conflict",
        request=httpx.Request("PATCH", "http://x/issues/i1"),
        response=httpx.Response(409, request=httpx.Request("PATCH", "http://x/issues/i1")),
    )

    # 1st get: no thread. 2nd get (in conflict handler): winner visible.
    # 3rd get (post-append verify): still the winner.
    mock_get = AsyncMock(side_effect=[
        {"id": "i1", "title": "Bug", "thread_id": None},
        {"id": "i1", "title": "Bug", "thread_id": "th-winner"},
        {"id": "i1", "title": "Bug", "thread_id": "th-winner"},
    ])
    mock_create_thread = AsyncMock(return_value={"id": "th-mine"})
    mock_update = AsyncMock(side_effect=conflict)
    mock_append = AsyncMock(return_value={"id": "msg-c"})

    with patch.object(mod.SuperposClient, "get_issue", mock_get), \
         patch.object(mod.SuperposClient, "create_thread", mock_create_thread), \
         patch.object(mod.SuperposClient, "update_issue", mock_update), \
         patch.object(mod.SuperposClient, "append_thread_message", mock_append), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["comment", "--issue-id", "i1", "--message", "hi"])
        rc = await mod._run(args)

    assert rc == 0
    mock_update.assert_awaited_once_with("i1", thread_id="th-mine")
    # conflict swallowed; message lands in the winning thread, once
    mock_append.assert_awaited_once_with("th-winner", "hi")


@pytest.mark.asyncio
async def test_comment_reappends_into_winner_on_later_overwrite(monkeypatch):
    """Regression for the later-overwrite interleaving: our link wins at refetch
    time and we append into it, but a concurrent writer overwrites the issue's
    ``thread_id`` *after* that — orphaning our thread. The post-append verify
    must detect the displacement and re-append into the new winner so the
    comment stays visible in ``discussion`` (not only the easier case where the
    winner is already visible before our refetch)."""
    mod = _load_script()
    _set_env(monkeypatch)

    # it1 resolve-start: no thread. it1 resolve-end refetch: our link (th-mine)
    # won. it1 post-append verify: a racing writer has since replaced it with
    # th-winner → displaced. it2 resolve-start: th-winner present. it2 verify:
    # still th-winner → converged.
    mock_get = AsyncMock(side_effect=[
        {"id": "i1", "title": "Bug", "thread_id": None},
        {"id": "i1", "title": "Bug", "thread_id": "th-mine"},
        {"id": "i1", "title": "Bug", "thread_id": "th-winner"},
        {"id": "i1", "title": "Bug", "thread_id": "th-winner"},
        {"id": "i1", "title": "Bug", "thread_id": "th-winner"},
    ])
    mock_create_thread = AsyncMock(return_value={"id": "th-mine"})
    mock_update = AsyncMock(return_value={"id": "i1", "thread_id": "th-mine"})
    mock_append = AsyncMock(side_effect=[{"id": "msg-a"}, {"id": "msg-b"}])

    with patch.object(mod.SuperposClient, "get_issue", mock_get), \
         patch.object(mod.SuperposClient, "create_thread", mock_create_thread), \
         patch.object(mod.SuperposClient, "update_issue", mock_update), \
         patch.object(mod.SuperposClient, "append_thread_message", mock_append), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["comment", "--issue-id", "i1", "--message", "hi"])
        rc = await mod._run(args)

    assert rc == 0
    # appended first into our (later-orphaned) thread, then re-appended into the
    # authoritative winner once the overwrite is detected.
    assert mock_append.await_args_list == [call("th-mine", "hi"), call("th-winner", "hi")]
    # the second create/link is skipped on retry: the winner is already linked.
    mock_create_thread.assert_awaited_once()
    mock_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_comment_reuses_existing_thread(monkeypatch):
    """When the issue already has a thread, no thread is created/linked —
    the message is appended directly."""
    mod = _load_script()
    _set_env(monkeypatch)

    mock_get = AsyncMock(return_value={"id": "i1", "title": "Bug", "thread_id": "th-existing"})
    mock_create_thread = AsyncMock()
    mock_update = AsyncMock()
    mock_append = AsyncMock(return_value={"id": "msg-2"})

    with patch.object(mod.SuperposClient, "get_issue", mock_get), \
         patch.object(mod.SuperposClient, "create_thread", mock_create_thread), \
         patch.object(mod.SuperposClient, "update_issue", mock_update), \
         patch.object(mod.SuperposClient, "append_thread_message", mock_append), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["comment", "--issue-id", "i1", "--message", "again"])
        rc = await mod._run(args)

    assert rc == 0
    mock_create_thread.assert_not_awaited()
    mock_update.assert_not_awaited()
    mock_append.assert_awaited_once_with("th-existing", "again")


# ── dispatch: discussion ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discussion_prints_history_for_existing_thread(monkeypatch, capsys):
    mod = _load_script()
    _set_env(monkeypatch)

    mock_get = AsyncMock(return_value={"id": "i1", "thread_id": "th-1"})
    mock_thread = AsyncMock(return_value={"id": "th-1", "messages": [{"id": "m1", "message": "hello"}]})

    with patch.object(mod.SuperposClient, "get_issue", mock_get), \
         patch.object(mod.SuperposClient, "get_thread", mock_thread), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["discussion", "--issue-id", "i1"])
        rc = await mod._run(args)

    assert rc == 0
    mock_thread.assert_awaited_once_with("th-1")
    out = capsys.readouterr().out
    assert "hello" in out


@pytest.mark.asyncio
async def test_discussion_no_thread_prints_marker(monkeypatch, capsys):
    mod = _load_script()
    _set_env(monkeypatch)

    mock_get = AsyncMock(return_value={"id": "i1", "thread_id": None})
    mock_thread = AsyncMock()

    with patch.object(mod.SuperposClient, "get_issue", mock_get), \
         patch.object(mod.SuperposClient, "get_thread", mock_thread), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["discussion", "--issue-id", "i1"])
        rc = await mod._run(args)

    assert rc == 0
    mock_thread.assert_not_awaited()
    out = capsys.readouterr().out
    assert "No discussion yet" in out
