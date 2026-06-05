"""Unit tests for the bundled superpos-github CLI's agent-tagging behavior
in the ``pr-create`` subcommand.

The script ships without a ``.py`` extension, so we load it via importlib
from the source tree (not the pip-installed bundled copy) and exercise
``_apply_agent_tag`` and the ``pr-create`` dispatch with ``SuperposClient``
methods mocked — no network. Mirrors ``test_issues_module_script.py``.
"""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

# Load the source-tree copy so the test exercises the new code under
# development, not the (potentially older) pip-installed bundled copy.
_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "superpos_agent_core" / "modules"
    / "superpos-github" / "scripts" / "superpos-github"
)


def _load_script():
    loader = SourceFileLoader("_superpos_github_cli", str(_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _set_env(monkeypatch):
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")


# ── _apply_agent_tag ────────────────────────────────────────────────────


def test_apply_agent_tag_returns_unchanged_when_env_unset(monkeypatch):
    monkeypatch.delenv("SUPERPOS_AGENT_NAME", raising=False)
    mod = _load_script()
    title, body = mod._apply_agent_tag("fix: something", "Body text")
    assert title == "fix: something"
    assert body == "Body text"


def test_apply_agent_tag_returns_unchanged_when_env_empty(monkeypatch):
    monkeypatch.setenv("SUPERPOS_AGENT_NAME", "   ")
    mod = _load_script()
    title, body = mod._apply_agent_tag("fix: something", "Body text")
    assert title == "fix: something"
    assert body == "Body text"


def test_apply_agent_tag_prepends_title_and_appends_footer(monkeypatch):
    monkeypatch.setenv("SUPERPOS_AGENT_NAME", "claude-agent")
    mod = _load_script()
    title, body = mod._apply_agent_tag("fix: something", "Body text")
    assert title == "[claude-agent] fix: something"
    assert body.startswith("Body text")
    assert "> 🤖 **Authored by:** `claude-agent`" in body
    assert "`superpos-agent-app[bot]`" in body


def test_apply_agent_tag_handles_empty_title(monkeypatch):
    monkeypatch.setenv("SUPERPOS_AGENT_NAME", "gemini-agent")
    mod = _load_script()
    title, body = mod._apply_agent_tag("", "Body")
    assert title == "[gemini-agent]"


def test_apply_agent_tag_handles_empty_body(monkeypatch):
    monkeypatch.setenv("SUPERPOS_AGENT_NAME", "codex-agent")
    mod = _load_script()
    title, body = mod._apply_agent_tag("fix: x", "")
    assert title == "[codex-agent] fix: x"
    assert body.startswith("\n\n---\n")
    assert "> 🤖 **Authored by:** `codex-agent`" in body


def test_apply_agent_tag_does_not_double_tag(monkeypatch):
    """If the user already prefixed the title with the tag, don't add it twice."""
    monkeypatch.setenv("SUPERPOS_AGENT_NAME", "claude-agent")
    mod = _load_script()
    title, body = mod._apply_agent_tag("[claude-agent] fix: x", "Body")
    assert title == "[claude-agent] fix: x"
    # Body footer is still added — that's the visible signal and idempotent.


# ── arg parsing ─────────────────────────────────────────────────────────


def test_parser_pr_create_accepts_no_agent_tag():
    mod = _load_script()
    args = mod._build_parser().parse_args(
        ["pr-create", "o", "r", "--title", "t", "--head", "h", "--base", "b", "--no-agent-tag"]
    )
    assert args.cmd == "pr-create"
    assert args.no_agent_tag is True


def test_parser_pr_create_no_agent_tag_defaults_false():
    mod = _load_script()
    args = mod._build_parser().parse_args(
        ["pr-create", "o", "r", "--title", "t", "--head", "h", "--base", "b"]
    )
    assert args.no_agent_tag is False


# ── dispatch ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_create_tags_title_and_body_when_env_set(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    monkeypatch.setenv("SUPERPOS_AGENT_NAME", "claude-agent")

    # service_request is awaited once for the pr-create POST.
    fake_resp = AsyncMock()
    fake_resp.json = Mock(return_value={"number": 42, "html_url": "https://gh/x"})
    fake_resp.status_code = 201
    mock_service_request = AsyncMock(return_value=fake_resp)

    with patch.object(mod.SuperposClient, "service_request", mock_service_request), \
         patch.object(mod.SuperposClient, "list_github_connections", AsyncMock(return_value=[{"name": "svc"}])), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(
            ["pr-create", "o", "r", "--title", "fix: x", "--head", "h", "--base", "b",
             "--body", "Body"]
        )
        rc = await mod._run(args)

    assert rc == 0
    # Pull the body the proxy was called with.
    assert mock_service_request.await_count == 1
    call_kwargs = mock_service_request.await_args.kwargs
    sent_body = call_kwargs["json"]
    assert sent_body["title"] == "[claude-agent] fix: x"
    assert "Body" in sent_body["body"]
    assert "> 🤖 **Authored by:** `claude-agent`" in sent_body["body"]


@pytest.mark.asyncio
async def test_pr_create_respects_no_agent_tag_flag(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    monkeypatch.setenv("SUPERPOS_AGENT_NAME", "claude-agent")

    fake_resp = AsyncMock()
    fake_resp.json = Mock(return_value={"number": 42})
    fake_resp.status_code = 201
    mock_service_request = AsyncMock(return_value=fake_resp)

    with patch.object(mod.SuperposClient, "service_request", mock_service_request), \
         patch.object(mod.SuperposClient, "list_github_connections", AsyncMock(return_value=[{"name": "svc"}])), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(
            ["pr-create", "o", "r", "--title", "fix: x", "--head", "h", "--base", "b",
             "--body", "Body", "--no-agent-tag"]
        )
        rc = await mod._run(args)

    assert rc == 0
    sent_body = mock_service_request.await_args.kwargs["json"]
    assert sent_body["title"] == "fix: x"
    assert sent_body["body"] == "Body"


@pytest.mark.asyncio
async def test_pr_create_no_tag_when_env_unset(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    monkeypatch.delenv("SUPERPOS_AGENT_NAME", raising=False)

    fake_resp = AsyncMock()
    fake_resp.json = Mock(return_value={"number": 42})
    fake_resp.status_code = 201
    mock_service_request = AsyncMock(return_value=fake_resp)

    with patch.object(mod.SuperposClient, "service_request", mock_service_request), \
         patch.object(mod.SuperposClient, "list_github_connections", AsyncMock(return_value=[{"name": "svc"}])), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(
            ["pr-create", "o", "r", "--title", "fix: x", "--head", "h", "--base", "b",
             "--body", "Body"]
        )
        rc = await mod._run(args)

    assert rc == 0
    sent_body = mock_service_request.await_args.kwargs["json"]
    assert sent_body["title"] == "fix: x"
    assert sent_body["body"] == "Body"
