"""Unit tests for the bundled superpos-registry CLI.

The script ships without a ``.py`` extension (it's a PATH executable), so we
load it via importlib from the bundled modules dir and exercise its arg
parsing and request-dispatch logic with ``SuperposClient`` methods mocked —
no network. Mirrors ``test_tracks_module_script.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from superpos_agent_core import bundled_modules_dir

_SCRIPT = (
    Path(bundled_modules_dir())
    / "superpos-registry" / "scripts" / "superpos-registry"
)


def _load_script():
    loader = SourceFileLoader("_superpos_registry_cli", str(_SCRIPT))
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


def test_parser_requires_kind():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["list"])  # missing --kind


def test_parser_rejects_unknown_kind():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["list", "--kind", "plugin"])


def test_parser_create_requires_slug_and_name():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["create", "--kind", "skill", "--name", "X"])
    with pytest.raises(SystemExit):
        parser.parse_args(["create", "--kind", "skill", "--slug", "x"])
    args = parser.parse_args(
        ["create", "--kind", "skill", "--slug", "x", "--name", "X"],
    )
    assert args.cmd == "create" and args.kind == "skill" and args.slug == "x"


# ── dispatch (mocked client) ────────────────────────────────────────────


async def test_create_dispatches_with_payload_and_body(monkeypatch, tmp_path):
    _set_env(monkeypatch)
    mod = _load_script()
    body_file = tmp_path / "SKILL.md"
    body_file.write_text("# Deep dive\n\nDo the thing.")
    mock_create = AsyncMock(return_value={"id": "r1", "slug": "deep-dive"})

    with patch.object(mod.SuperposClient, "create_registry_item", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args([
            "create", "--kind", "skill", "--slug", "deep-dive",
            "--name", "Deep Dive",
            "--payload", '{"frontmatter": {"name": "Deep Dive"}, "files": []}',
            "--body-file", str(body_file),
            "--message", "initial",
        ]))

    assert rc == 0
    assert mock_create.await_args.args == ("skill", "deep-dive")
    kw = mock_create.await_args.kwargs
    assert kw["name"] == "Deep Dive"
    # --body-file populated payload.instructions on top of the JSON payload
    assert kw["payload"] == {
        "frontmatter": {"name": "Deep Dive"},
        "files": [],
        "instructions": "# Deep dive\n\nDo the thing.",
    }
    assert kw["message"] == "initial"
    assert kw["visibility"] is None


async def test_create_private_sets_visibility(monkeypatch):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_create = AsyncMock(return_value={"id": "r1"})

    with patch.object(mod.SuperposClient, "create_registry_item", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args([
            "create", "--kind", "module", "--slug", "foo", "--name", "Foo",
            "--payload", '{"manifest": {}}', "--private",
        ]))

    assert mock_create.await_args.kwargs["visibility"] == "private"


async def test_create_without_payload_or_body_errors(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_create = AsyncMock()

    with patch.object(mod.SuperposClient, "create_registry_item", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args([
            "create", "--kind", "skill", "--slug", "x", "--name", "X",
        ]))

    assert rc == 2
    assert not mock_create.called
    assert "needs a payload" in capsys.readouterr().err


async def test_update_sends_only_changed_fields(monkeypatch):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_update = AsyncMock(return_value={"id": "r1"})

    with patch.object(mod.SuperposClient, "update_registry_item", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args([
            "update", "--kind", "skill", "--slug", "deep-dive", "--draft",
        ]))

    assert mock_update.await_args.args == ("skill", "deep-dive")
    kw = mock_update.await_args.kwargs
    assert kw["is_active"] is False
    assert kw["name"] is None and kw["payload"] is None


async def test_update_with_no_fields_errors(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_update = AsyncMock()

    with patch.object(mod.SuperposClient, "update_registry_item", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args([
            "update", "--kind", "skill", "--slug", "deep-dive",
        ]))

    assert rc == 2
    assert not mock_update.called
    assert "at least one field" in capsys.readouterr().err


async def test_update_is_active_and_draft_conflict_errors(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_update = AsyncMock()

    with patch.object(mod.SuperposClient, "update_registry_item", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        with pytest.raises(SystemExit) as exc:
            await mod._run(mod._build_parser().parse_args([
                "update", "--kind", "skill", "--slug", "x",
                "--is-active", "--draft",
            ]))

    assert exc.value.code == 2
    assert not mock_update.called
    assert "mutually exclusive" in capsys.readouterr().err


async def test_delete_dispatches(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_delete = AsyncMock(return_value=None)

    with patch.object(mod.SuperposClient, "delete_registry_item", mock_delete), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args([
            "delete", "--kind", "dynamic_workflow", "--slug", "nightly",
        ]))

    assert rc == 0
    assert mock_delete.await_args.args == ("dynamic_workflow", "nightly")
    out = json.loads(capsys.readouterr().out)
    assert out == {"deleted": True, "kind": "dynamic_workflow", "slug": "nightly"}


async def test_show_dispatches(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_show = AsyncMock(return_value={"slug": "deep-dive", "payload": {}})

    with patch.object(mod.SuperposClient, "get_registry_item", mock_show), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args([
            "show", "--kind", "skill", "--slug", "deep-dive",
        ]))

    assert rc == 0
    assert mock_show.await_args.args == ("skill", "deep-dive")
    assert json.loads(capsys.readouterr().out)["slug"] == "deep-dive"


async def test_list_forwards_include_flags(monkeypatch):
    _set_env(monkeypatch)
    mod = _load_script()
    mock_list = AsyncMock(return_value=[])

    with patch.object(mod.SuperposClient, "list_registry_items", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args([
            "list", "--kind", "skill", "--include-inactive", "--include-deleted",
        ]))

    assert mock_list.await_args.args == ("skill",)
    assert mock_list.await_args.kwargs == {
        "include_inactive": True, "include_deleted": True,
    }


# ── env validation ──────────────────────────────────────────────────────


def test_main_errors_when_env_missing(monkeypatch, capsys):
    monkeypatch.delenv("SUPERPOS_BASE_URL", raising=False)
    monkeypatch.delenv("SUPERPOS_HIVE_ID", raising=False)
    monkeypatch.delenv("SUPERPOS_API_TOKEN", raising=False)
    mod = _load_script()
    with pytest.raises(SystemExit) as exc:
        mod.main(["list", "--kind", "skill"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "SUPERPOS_BASE_URL" in err


# ── doc / summary accuracy ──────────────────────────────────────────────


def _registered_subcommands(mod):
    parser = mod._build_parser()
    sub_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return set(sub_action.choices)


def test_docstring_covers_every_subcommand():
    mod = _load_script()
    doc = mod.__doc__ or ""
    missing = [cmd for cmd in _registered_subcommands(mod) if cmd not in doc]
    assert not missing, f"docstring omits subcommands: {missing}"
