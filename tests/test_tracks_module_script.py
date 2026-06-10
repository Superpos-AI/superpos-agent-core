"""Unit tests for the bundled superpos-tracks CLI.

The script ships without a ``.py`` extension (it's a PATH executable), so we
load it via importlib from the bundled modules dir and exercise its arg
parsing and request-dispatch logic with ``SuperposClient`` methods mocked —
no network.  Mirrors ``test_issues_module_script.py``.
"""

from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from superpos_agent_core import bundled_modules_dir

_SCRIPT = (
    Path(bundled_modules_dir())
    / "superpos-tracks" / "scripts" / "superpos-tracks"
)


def _load_script():
    loader = SourceFileLoader("_superpos_tracks_cli", str(_SCRIPT))
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


def test_parser_list_accepts_status_and_tag():
    mod = _load_script()
    parser = mod._build_parser()
    args = parser.parse_args(["list", "--status", "active", "--tag", "infra"])
    assert args.cmd == "list"
    assert args.status == "active"
    assert args.tag == "infra"


def test_parser_list_rejects_unknown_status():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["list", "--status", "bogus"])


def test_parser_get_takes_positional_slug():
    mod = _load_script()
    args = mod._build_parser().parse_args(["get", "k1"])
    assert args.cmd == "get" and args.slug == "k1"


def test_parser_create_requires_slug_and_title():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["create", "--title", "Knowledge Wiki"])
    with pytest.raises(SystemExit):
        parser.parse_args(["create", "--slug", "k1"])
    args = parser.parse_args(["create", "--slug", "k1", "--title", "Knowledge Wiki"])
    assert args.cmd == "create" and args.slug == "k1" and args.title == "Knowledge Wiki"


def test_parser_create_accepts_status_and_spec_file():
    mod = _load_script()
    args = mod._build_parser().parse_args([
        "create", "--slug", "k1", "--title", "Knowledge Wiki",
        "--status", "active", "--spec-file", "/tmp/spec.md",
    ])
    assert args.status == "active" and args.spec_file == "/tmp/spec.md"


def test_parser_patch_takes_positional_slug():
    mod = _load_script()
    args = mod._build_parser().parse_args(["patch", "k1", "--title", "New name"])
    assert args.cmd == "patch" and args.slug == "k1" and args.title == "New name"


def test_parser_link_issue_takes_two_positionals():
    mod = _load_script()
    args = mod._build_parser().parse_args(["link-issue", "k1", "i1"])
    assert args.cmd == "link-issue" and args.track_slug == "k1" and args.issue_id == "i1"


def test_parser_add_issue_is_alias_for_link_issue():
    mod = _load_script()
    args = mod._build_parser().parse_args(["add-issue", "k1", "i1"])
    assert args.cmd == "add-issue" and args.track_slug == "k1" and args.issue_id == "i1"


def test_parser_unlink_issue_takes_two_positionals():
    mod = _load_script()
    args = mod._build_parser().parse_args(["unlink-issue", "k1", "i1"])
    assert args.cmd == "unlink-issue" and args.track_slug == "k1" and args.issue_id == "i1"


# ── dispatch (mocked client) ────────────────────────────────────────────


async def test_list_dispatches_to_list_tracks(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_list = AsyncMock(return_value=[{"slug": "k1"}])

    with patch.object(mod.SuperposClient, "list_tracks", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args(["list"]))

    assert rc == 0
    assert mock_list.await_count == 1
    assert mock_list.await_args.kwargs == {"status": None, "tag": None}
    out = json.loads(capsys.readouterr().out)
    assert out == [{"slug": "k1"}]


async def test_list_dispatches_status_and_tag_filters(monkeypatch):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_list = AsyncMock(return_value=[])

    with patch.object(mod.SuperposClient, "list_tracks", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args(
            ["list", "--status", "active", "--tag", "infra"],
        ))

    assert mock_list.await_args.kwargs == {"status": "active", "tag": "infra"}


async def test_get_dispatches_to_get_track_by_slug(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_get = AsyncMock(return_value={"slug": "k1", "spec": "..."})

    with patch.object(mod.SuperposClient, "get_track_by_slug", mock_get), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args(["get", "k1"]))

    assert rc == 0
    assert mock_get.await_args.args == ("k1",)
    out = json.loads(capsys.readouterr().out)
    assert out["slug"] == "k1"


async def test_create_dispatches_to_create_track(monkeypatch, capsys, tmp_path):
    _set_env(monkeypatch)
    mod = _load_script()
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("## Status\n\nActive.")
    mock_create = AsyncMock(return_value={"slug": "k1", "name": "Knowledge Wiki"})

    with patch.object(mod.SuperposClient, "create_track", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args([
            "create",
            "--slug", "k1",
            "--title", "Knowledge Wiki",
            "--description", "Karpathy-style typed pages",
            "--spec-file", str(spec_file),
            "--status", "active",
        ]))

    assert rc == 0
    assert mock_create.await_args.kwargs == {
        "slug": "k1",
        "name": "Knowledge Wiki",
        "description": "Karpathy-style typed pages",
        "spec": "## Status\n\nActive.",
        "state": "active",
    }


async def test_create_rejects_unknown_status(monkeypatch, capsys):
    """argparse's ``choices`` guard rejects an unknown state at parse time —
    the dispatch is never reached and ``create_track`` is not called."""
    _set_env(monkeypatch)
    mod = _load_script()
    mock_create = AsyncMock()

    with patch.object(mod.SuperposClient, "create_track", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        with pytest.raises(SystemExit) as exc:
            await mod._run(mod._build_parser().parse_args([
                "create", "--slug", "k1", "--title", "Knowledge Wiki", "--status", "bogus",
            ]))

    assert exc.value.code == 2
    assert not mock_create.called
    err = capsys.readouterr().err
    assert "invalid choice: 'bogus'" in err


async def test_create_without_spec_file_leaves_spec_none(monkeypatch):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_create = AsyncMock(return_value={"slug": "k1"})

    with patch.object(mod.SuperposClient, "create_track", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args([
            "create", "--slug", "k1", "--title", "Knowledge Wiki",
        ]))

    assert mock_create.await_args.kwargs["spec"] is None
    assert mock_create.await_args.kwargs["state"] is None


async def test_patch_dispatches_to_patch_track_with_only_set_fields(monkeypatch, tmp_path):
    _set_env(monkeypatch)
    mod = _load_script()
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("## New spec")
    mock_patch = AsyncMock(return_value={"slug": "k1"})

    with patch.object(mod.SuperposClient, "patch_track", mock_patch), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args([
            "patch", "k1", "--title", "New name", "--spec-file", str(spec_file),
        ]))

    assert mock_patch.await_args.args == ("k1",)
    assert mock_patch.await_args.kwargs == {
        "name": "New name", "spec": "## New spec",
    }


async def test_patch_with_no_fields_errors_out(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_patch = AsyncMock()

    with patch.object(mod.SuperposClient, "patch_track", mock_patch), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args(["patch", "k1"]))

    assert rc == 2
    assert not mock_patch.called
    err = capsys.readouterr().err
    assert "patch requires at least one of" in err


async def test_link_issue_dispatches_to_link_track_issue(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_link = AsyncMock(return_value={"track_id": "t1", "issue_id": "i1"})

    with patch.object(mod.SuperposClient, "link_track_issue", mock_link), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args(["link-issue", "k1", "i1"]))

    assert rc == 0
    assert mock_link.await_args.args == ("k1", "i1")
    out = json.loads(capsys.readouterr().out)
    assert out == {"track_id": "t1", "issue_id": "i1"}


async def test_add_issue_dispatches_to_link_track_issue(monkeypatch):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_link = AsyncMock(return_value={"track_id": "t1", "issue_id": "i1"})

    with patch.object(mod.SuperposClient, "link_track_issue", mock_link), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args(["add-issue", "k1", "i1"]))

    # add-issue is an alias — must call the same method
    assert mock_link.await_args.args == ("k1", "i1")


async def test_unlink_issue_dispatches_to_unlink_track_issue(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_unlink = AsyncMock(return_value=None)

    with patch.object(mod.SuperposClient, "unlink_track_issue", mock_unlink), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args(["unlink-issue", "k1", "i1"]))

    assert rc == 0
    assert mock_unlink.await_args.args == ("k1", "i1")
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "track_slug": "k1", "issue_id": "i1"}


# ── env validation ──────────────────────────────────────────────────────


def test_main_errors_when_env_missing(monkeypatch, capsys):
    monkeypatch.delenv("SUPERPOS_BASE_URL", raising=False)
    monkeypatch.delenv("SUPERPOS_HIVE_ID", raising=False)
    monkeypatch.delenv("SUPERPOS_API_TOKEN", raising=False)
    mod = _load_script()
    with pytest.raises(SystemExit) as exc:
        mod.main(["list"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "SUPERPOS_BASE_URL" in err and "SUPERPOS_HIVE_ID" in err
