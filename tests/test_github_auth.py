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

from superpos_agent_core import GitHubDiscoveryForbidden, github_auth as ga


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


async def test_mint_token_reuses_cache_for_same_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERPOS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SUPERPOS_GITHUB_CONNECTION_ID", "conn-1")
    cache = ga._token_cache_path()
    cache.write_text(
        json.dumps(
            {"token": "cached_tok", "expires_at": _iso(3600), "connection_id": "conn-1"}
        )
    )
    # No SUPERPOS_BASE_URL → constructing a client would blow up; reusing the
    # cache must short-circuit before any network path.
    monkeypatch.delenv("SUPERPOS_BASE_URL", raising=False)
    assert await ga._mint_token() == "cached_tok"


class _FakeMintClient:
    def __init__(self, *args, **kwargs):
        pass

    async def mint_github_token(self, conn_id):
        return {"token": f"tok_for_{conn_id}", "expires_at": _iso(3600)}

    async def close(self):
        pass


async def test_mint_token_skips_cache_for_other_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERPOS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://hive.example")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive-1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "api-tok")
    monkeypatch.setenv("SUPERPOS_GITHUB_CONNECTION_ID", "conn-new")
    cache = ga._token_cache_path()
    cache.write_text(
        json.dumps(
            {"token": "stale_tok", "expires_at": _iso(3600), "connection_id": "conn-old"}
        )
    )
    monkeypatch.setattr(ga, "SuperposClient", _FakeMintClient)

    assert await ga._mint_token() == "tok_for_conn-new"
    # The cache is rewritten and now belongs to the connection we actually used.
    assert json.loads(cache.read_text())["connection_id"] == "conn-new"


async def test_mint_token_ignores_legacy_cache_without_connection_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERPOS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://hive.example")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive-1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "api-tok")
    monkeypatch.setenv("SUPERPOS_GITHUB_CONNECTION_ID", "conn-1")
    cache = ga._token_cache_path()
    cache.write_text(json.dumps({"token": "legacy_tok", "expires_at": _iso(3600)}))
    monkeypatch.setattr(ga, "SuperposClient", _FakeMintClient)

    assert await ga._mint_token() == "tok_for_conn-1"


def test_token_cache_is_installation_wide(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERPOS_STATE_DIR", str(tmp_path))
    # A single installation-wide cache file — no per-repo variants, because the
    # broker issues installation-wide tokens rather than repo-scoped ones.
    assert ga._token_cache_path().name == "token.json"


# ── credential helper protocol ──────────────────────────────────────────


def _run_credential(monkeypatch, stdin_text):
    out = io.StringIO()
    monkeypatch.setattr(ga.sys, "stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(ga.sys, "stdout", out)
    rc = ga.cmd_credential("get")
    return rc, out.getvalue()


def test_credential_get_emits_token_for_github(monkeypatch):
    calls = {"n": 0}

    async def fake_mint():
        calls["n"] += 1
        return "TKN123"

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    # A repo path is supplied, but the helper must ignore it: the broker token
    # is installation-wide, so minting takes no repo argument.
    rc, out = _run_credential(
        monkeypatch, "protocol=https\nhost=github.com\npath=acme/widgets.git\n\n"
    )

    assert rc == 0
    assert "username=x-access-token" in out
    assert "password=TKN123" in out
    assert calls["n"] == 1


def test_credential_get_ignores_other_hosts(monkeypatch):
    async def fake_mint():  # pragma: no cover - must not be called
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
    assert ga.cmd_token() == 0
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

    async def fake_mint():
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


def test_setup_honours_connection_id_override_when_discovery_fails(monkeypatch):
    # No static token; catalog discovery yields nothing (e.g. the agent lacks
    # services.read), but SUPERPOS_GITHUB_CONNECTION_ID pins a connection the
    # broker can still mint from. Setup must configure auth from the override
    # rather than bailing at the ``if not conn`` branch.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://hive.example")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive-1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "api-tok")
    monkeypatch.setenv("SUPERPOS_GITHUB_CONNECTION_ID", "conn-override")

    async def fail_resolve(client):  # pragma: no cover - must not be called
        raise AssertionError("override must short-circuit discovery")

    async def fake_mint():
        return "brokered_tok"

    monkeypatch.setattr(ga, "_resolve_app_connection", fail_resolve)
    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    monkeypatch.setattr(ga, "_configure_app_credential_helper", lambda: None)
    calls = _record_subprocess(monkeypatch)

    assert ga.cmd_setup() == 0

    login = [c for c in calls if c["cmd"][:4] == ["gh", "auth", "login", "--with-token"]]
    assert len(login) == 1
    assert login[0]["input"] == "brokered_tok"


def test_setup_app_path_warns_when_mint_fails(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://hive.example")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive-1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "api-tok")

    async def fake_resolve(client):
        return {"id": "conn-1", "name": "acme-app"}

    async def fake_mint():
        return None

    monkeypatch.setattr(ga, "_resolve_app_connection", fake_resolve)
    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    monkeypatch.setattr(ga, "_configure_app_credential_helper", lambda: None)
    calls = _record_subprocess(monkeypatch)

    assert ga.cmd_setup() == 0
    # No gh login attempted when there is no token to hand it.
    assert all(c["cmd"][:3] != ["gh", "auth", "login"] for c in calls)


# ── _resolve_app_connection on permission denial ────────────────────────


async def test_resolve_app_connection_returns_none_on_forbidden(monkeypatch):
    # A permission denial must not propagate — callers fall through to the
    # static GITHUB_TOKEN path, and setup logs the "no connection" message.
    class _ForbiddenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def list_github_connections(self):
            raise GitHubDiscoveryForbidden(
                403,
                "Agent lacks `services.read` permission — cannot list "
                "GitHub service connections",
            )

        async def close(self):
            pass

    monkeypatch.setattr(ga, "SuperposClient", _ForbiddenClient)

    result = await ga._resolve_app_connection(_ForbiddenClient())  # type: ignore[arg-type]
    assert result is None
