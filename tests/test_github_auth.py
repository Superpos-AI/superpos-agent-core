"""Tests for the GitHub credential bootstrap (github_auth).

Network is never touched: the token-cache path is exercised with a pre-written
fresh cache, and the credential-helper / token commands are driven with
``_mint_token`` monkeypatched.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from superpos_agent_core import github_auth as ga


def _iso(delta_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    ).isoformat()


# ── _is_fresh ───────────────────────────────────────────────────────────


def test_is_fresh_true_when_well_ahead():
    assert ga._is_fresh(_iso(3600)) is True


def test_is_fresh_false_within_skew_window():
    # Inside the re-mint skew window → treat as stale.
    assert ga._is_fresh(_iso(ga._EXPIRY_SKEW_SECONDS - 30)) is False


@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_is_fresh_false_on_missing_or_bad(value):
    assert ga._is_fresh(value) is False


# ── token cache reuse (no minting) ──────────────────────────────────────


async def test_mint_token_reuses_fresh_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERPOS_STATE_DIR", str(tmp_path))
    cache = ga._token_cache_path(None)
    cache.write_text(json.dumps({"token": "cached_tok", "expires_at": _iso(3600)}))

    # If it tried to mint, constructing SuperposClient with no env would blow up;
    # a clean return proves the cache short-circuited the network path.
    monkeypatch.delenv("SUPERPOS_BASE_URL", raising=False)
    assert await ga._mint_token(None) == "cached_tok"


# ── credential helper protocol ──────────────────────────────────────────


def _run_credential(monkeypatch, stdin_text):
    out = io.StringIO()
    monkeypatch.setattr(ga.sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(ga.sys, "stdout", out)
    rc = ga.cmd_credential("get")
    return rc, out.getvalue()


def test_credential_get_emits_token_for_github(monkeypatch):
    seen = {}

    async def fake_mint(repo):
        seen["repo"] = repo
        return "TKN123"

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    rc, out = _run_credential(
        monkeypatch, "protocol=https\nhost=github.com\npath=acme/widgets.git\n\n"
    )

    assert rc == 0
    assert "username=x-access-token" in out
    assert "password=TKN123" in out
    # useHttpPath gives us owner/repo with the .git stripped → repo-scoped mint.
    assert seen["repo"] == "acme/widgets"


def test_credential_get_ignores_other_hosts(monkeypatch):
    async def fake_mint(repo):  # pragma: no cover - must not be called
        raise AssertionError("should not mint for non-github host")

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    rc, out = _run_credential(monkeypatch, "protocol=https\nhost=gitlab.com\n\n")
    assert rc == 0
    assert out == ""


def test_credential_store_and_erase_are_noops():
    assert ga.cmd_credential("store") == 0
    assert ga.cmd_credential("erase") == 0


# ── token command honours static GITHUB_TOKEN ───────────────────────────


def test_token_command_prefers_static(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_static")
    assert ga.cmd_token(None) == 0
    assert capsys.readouterr().out == "ghp_static"


# ── setup wires gh in the github_app path ───────────────────────────────


def _record_subprocess(monkeypatch):
    """Capture subprocess.run invocations without touching the system."""
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "input": kwargs.get("input")})

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(ga.subprocess, "run", fake_run)
    return calls


def test_setup_static_token_logs_in_gh_and_sets_up_git(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_static")
    calls = _record_subprocess(monkeypatch)

    assert ga.cmd_setup() == 0

    joined = [" ".join(c["cmd"]) for c in calls]
    assert any("gh auth login --with-token" in j for j in joined)
    assert any("gh auth setup-git" in j for j in joined)


def test_setup_app_path_authenticates_gh_with_minted_token(monkeypatch):
    # No static token, but a github_app connection resolves and mints a token.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://hive.example")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive-1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "api-tok")

    async def fake_resolve(client):
        return {"id": "conn-1", "name": "acme-app"}

    async def fake_mint(repo):
        assert repo is None  # gh login uses an org-scoped token
        return "brokered_tok"

    monkeypatch.setattr(ga, "_resolve_app_connection", fake_resolve)
    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    monkeypatch.setattr(ga, "_configure_app_credential_helper", lambda: None)
    calls = _record_subprocess(monkeypatch)

    assert ga.cmd_setup() == 0

    login = [c for c in calls if c["cmd"][:4] == ["gh", "auth", "login", "--with-token"]]
    assert len(login) == 1
    assert login[0]["input"] == "brokered_tok"
    # git stays on our credential helper — gh must not reclaim it.
    assert all(c["cmd"][:3] != ["gh", "auth", "setup-git"] for c in calls)


def test_setup_app_path_warns_when_mint_fails(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://hive.example")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive-1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "api-tok")

    async def fake_resolve(client):
        return {"id": "conn-1", "name": "acme-app"}

    async def fake_mint(repo):
        return None

    monkeypatch.setattr(ga, "_resolve_app_connection", fake_resolve)
    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    monkeypatch.setattr(ga, "_configure_app_credential_helper", lambda: None)
    calls = _record_subprocess(monkeypatch)

    assert ga.cmd_setup() == 0
    # No gh login attempted when there is no token to hand it.
    assert all(c["cmd"][:3] != ["gh", "auth", "login"] for c in calls)
