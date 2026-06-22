"""Unit tests for the bundled superpos-hosted-agents CLI.

The script ships without a ``.py`` extension (it's a PATH executable), so we
load it via importlib from the bundled modules dir and exercise its arg
parsing and request-dispatch logic with ``SuperposClient`` methods mocked —
no network.  Mirrors ``test_tracks_module_script.py``.
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
    / "superpos-hosted-agents" / "scripts" / "superpos-hosted-agents"
)


def _load_script():
    loader = SourceFileLoader("_superpos_hosted_agents_cli", str(_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _set_env(monkeypatch):
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")


_SKILL = _SCRIPT.parent.parent / "SKILL.md"


# ── packaging / shape ────────────────────────────────────────────────────


def test_script_exists_and_is_executable():
    assert _SCRIPT.is_file()
    assert _SCRIPT.stat().st_mode & 0o111  # at least one execute bit


def test_skill_and_module_yaml_ship():
    assert _SKILL.is_file()
    assert (_SCRIPT.parents[1] / "module.yaml").is_file()


def test_help_runs(capsys):
    """`--help` exits 0 and prints the prog name (smoke test)."""
    mod = _load_script()
    with pytest.raises(SystemExit) as exc:
        mod._build_parser().parse_args(["--help"])
    assert exc.value.code == 0
    assert "superpos-hosted-agents" in capsys.readouterr().out


# ── docs accuracy ────────────────────────────────────────────────────────


def test_skill_doc_flags_cloud_only_and_deferred_create():
    doc = _SKILL.read_text(encoding="utf-8").lower()
    assert "cloud-only" in doc
    assert "pkv-4" in doc


def test_module_yaml_description_mentions_lifecycle():
    import yaml

    meta = yaml.safe_load(
        (_SCRIPT.parents[1] / "module.yaml").read_text(encoding="utf-8")
    )
    assert "hosted agents" in meta["description"].lower()


def _registered_subcommands(mod):
    parser = mod._build_parser()
    sub_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return set(sub_action.choices)


def test_all_expected_subcommands_registered():
    mod = _load_script()
    expected = {
        "list", "show", "status", "logs", "deployments",
        "start", "stop", "restart", "redeploy", "scale", "rollback",
        "delete", "presets",
    }
    assert _registered_subcommands(mod) == expected


def test_docstring_covers_every_subcommand():
    mod = _load_script()
    doc = mod.__doc__ or ""
    missing = [cmd for cmd in _registered_subcommands(mod) if cmd not in doc]
    assert not missing, f"docstring omits subcommands: {missing}"


# ── arg parsing ──────────────────────────────────────────────────────────


def test_parser_show_takes_positional_id():
    mod = _load_script()
    args = mod._build_parser().parse_args(["show", "h1"])
    assert args.cmd == "show" and args.id == "h1"


def test_parser_scale_requires_size_and_count():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["scale", "h1", "--size", "m"])
    with pytest.raises(SystemExit):
        parser.parse_args(["scale", "h1", "--count", "3"])
    args = parser.parse_args(["scale", "h1", "--size", "m", "--count", "3"])
    assert args.size == "m" and args.count == 3


def test_parser_scale_rejects_bad_size():
    mod = _load_script()
    with pytest.raises(SystemExit):
        mod._build_parser().parse_args(["scale", "h1", "--size", "xxl", "--count", "1"])


def test_parser_rollback_requires_deployment_id():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["rollback", "h1"])
    args = parser.parse_args(["rollback", "h1", "--deployment-id", "d9"])
    assert args.id == "h1" and args.deployment_id == "d9"


def test_parser_logs_accepts_window_flags():
    mod = _load_script()
    args = mod._build_parser().parse_args([
        "logs", "h1",
        "--start", "2026-06-22T10:00:00Z",
        "--end", "2026-06-22T11:00:00Z",
        "--limit", "200", "--direction", "backward",
        "--search", "err", "--pod", "p1",
    ])
    assert args.start == "2026-06-22T10:00:00Z"
    assert args.limit == 200 and args.direction == "backward"
    assert args.search == "err" and args.pod == "p1"


# ── dispatch (mocked client) ─────────────────────────────────────────────


async def test_list_dispatches_to_list_hosted_agents(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock = AsyncMock(return_value={"data": [{"id": "h1"}], "meta": {}})

    with patch.object(mod.SuperposClient, "list_hosted_agents", mock), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args(["list"]))

    assert rc == 0
    assert mock.await_args.kwargs == {}
    out = json.loads(capsys.readouterr().out)
    assert out["data"] == [{"id": "h1"}]


async def test_scale_dispatches_with_size_and_count(monkeypatch):
    _set_env(monkeypatch)
    mod = _load_script()
    mock = AsyncMock(return_value={"data": {"id": "h1"}})

    with patch.object(mod.SuperposClient, "scale_hosted_agent", mock), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args(
            ["scale", "h1", "--size", "l", "--count", "5"],
        ))

    assert mock.await_args.args == ("h1",)
    assert mock.await_args.kwargs == {"size": "l", "count": 5}


async def test_rollback_dispatches_with_deployment_id(monkeypatch):
    _set_env(monkeypatch)
    mod = _load_script()
    mock = AsyncMock(return_value={"data": {"id": "h1"}})

    with patch.object(mod.SuperposClient, "rollback_hosted_agent_deployment", mock), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args(
            ["rollback", "h1", "--deployment-id", "d9"],
        ))

    assert mock.await_args.args == ("h1", "d9")


async def test_logs_dispatches_only_set_flags(monkeypatch):
    _set_env(monkeypatch)
    mod = _load_script()
    mock = AsyncMock(return_value={"data": {}, "meta": {}})

    with patch.object(mod.SuperposClient, "get_hosted_agent_logs", mock), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        await mod._run(mod._build_parser().parse_args(
            ["logs", "h1", "--start", "2026-06-22T10:00:00Z",
             "--end", "2026-06-22T11:00:00Z"],
        ))

    assert mock.await_args.args == ("h1",)
    # Unset flags (limit/direction/search/pod) must be omitted, not None.
    assert mock.await_args.kwargs == {
        "start": "2026-06-22T10:00:00Z",
        "end": "2026-06-22T11:00:00Z",
    }


async def test_presets_dispatches_to_list_presets(monkeypatch, capsys):
    _set_env(monkeypatch)
    mod = _load_script()
    mock = AsyncMock(return_value=[{"key": "claude-sdk"}])

    with patch.object(mod.SuperposClient, "list_hosted_agent_presets", mock), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        rc = await mod._run(mod._build_parser().parse_args(["presets"]))

    assert rc == 0
    assert mock.await_count == 1
    out = json.loads(capsys.readouterr().out)
    assert out == [{"key": "claude-sdk"}]


# ── env validation ───────────────────────────────────────────────────────


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
