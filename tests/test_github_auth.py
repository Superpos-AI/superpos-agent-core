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
    calls = {"n": 0, "owner": "unset"}

    async def fake_mint(owner=None):
        calls["n"] += 1
        calls["owner"] = owner
        return "TKN123"

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    # The repo path is forwarded (useHttpPath=true); the helper parses the owner
    # from it and passes it to minting for owner-aware resolution.
    rc, out = _run_credential(
        monkeypatch, "protocol=https\nhost=github.com\npath=acme/widgets.git\n\n"
    )

    assert rc == 0
    assert "username=x-access-token" in out
    assert "password=TKN123" in out
    assert calls["n"] == 1
    assert calls["owner"] == "acme"


def test_credential_get_ignores_other_hosts(monkeypatch):
    async def fake_mint(owner=None):  # pragma: no cover - must not be called
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

        async def get_persona(self):
            # No persona github block available → nothing to fall back to.
            return None

        async def close(self):
            pass

    monkeypatch.setattr(ga, "SuperposClient", _ForbiddenClient)

    result = await ga._resolve_app_connection(_ForbiddenClient())  # type: ignore[arg-type]
    assert result is None


async def test_resolve_app_connection_falls_back_to_persona_on_forbidden(monkeypatch):
    # When the services.read catalog 403s, fall back to the persona github
    # block, which is scoped by services:{id} instead.
    class _PersonaFallbackClient:
        def __init__(self, *args, **kwargs):
            pass

        async def list_github_connections(self):
            raise GitHubDiscoveryForbidden(403, "no services.read")

        async def get_persona(self):
            return {"github": {
                "default_connection_id": "app-uuid",
                "connections": [
                    {"service_connection_id": "app-uuid", "name": "gh-app",
                     "broker_compatible": True},
                    {"service_connection_id": "pat-uuid", "name": "gh-pat",
                     "broker_compatible": False},
                ],
            }}

        async def close(self):
            pass

    monkeypatch.setattr(ga, "_write_json_private", lambda *a, **k: None)

    result = await ga._resolve_app_connection(_PersonaFallbackClient())  # type: ignore[arg-type]
    assert result == {"id": "app-uuid", "name": "gh-app"}


async def test_resolve_app_connection_persona_skips_pat_only(monkeypatch):
    # A persona block with only PAT (non-broker-compatible) connections yields
    # nothing — the broker can't mint from those, so we fall through to
    # GITHUB_TOKEN.
    class _PatOnlyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def list_github_connections(self):
            raise GitHubDiscoveryForbidden(403, "no services.read")

        async def get_persona(self):
            return {"github": {
                "default_connection_id": None,
                "connections": [
                    {"service_connection_id": "pat-uuid", "name": "gh-pat",
                     "broker_compatible": False},
                ],
            }}

        async def close(self):
            pass

    result = await ga._resolve_app_connection(_PatOnlyClient())  # type: ignore[arg-type]
    assert result is None


async def test_resolve_app_connection_persona_owner_aware_on_forbidden(monkeypatch):
    # Catalog 403s and the persona block holds two broker-compatible
    # connections; the owner selects the right one (owner-aware fallback).
    class _TwoBrokerClient:
        def __init__(self, *args, **kwargs):
            pass

        async def list_github_connections(self):
            raise GitHubDiscoveryForbidden(403, "no services.read")

        async def get_persona(self):
            return {"github": {
                "default_connection_id": None,
                "connections": [
                    {"service_connection_id": "conn-a", "name": "gh-a",
                     "broker_compatible": True, "target_login": "org-a"},
                    {"service_connection_id": "conn-b", "name": "gh-b",
                     "broker_compatible": True, "target_login": "org-b"},
                ],
            }}

        async def close(self):
            pass

    monkeypatch.setattr(ga, "_write_json_private", lambda *a, **k: None)

    result = await ga._resolve_app_connection(
        _TwoBrokerClient(), owner="org-b"  # type: ignore[arg-type]
    )
    assert result == {"id": "conn-b", "name": "gh-b"}

    # An owner matching none of two connections fails clear (no wrong-org mint).
    with pytest.raises(ga._AmbiguousConnection):
        await ga._resolve_app_connection(
            _TwoBrokerClient(), owner="nobody"  # type: ignore[arg-type]
        )


# ── owner parsing ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("acme/widgets.git", "acme"),
        ("acme/widgets", "acme"),
        ("/acme/widgets.git", "acme"),
        ("Address-SO/Repo.git", "Address-SO"),
        ("owner.git", "owner"),  # single segment, .git stripped
        (None, None),
        ("", None),
    ],
)
def test_owner_from_path(path, expected):
    assert ga._owner_from_path(path) == expected


# ── owner-aware connection resolution ───────────────────────────────────


def _persona_client(connections, default_id=None):
    """A SuperposClient stand-in whose persona block carries the given
    github connections + default_connection_id."""

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def get_persona(self):
            return {
                "github": {
                    "connections": connections,
                    "default_connection_id": default_id,
                }
            }

        async def close(self):
            pass

    return _Client


_TWO_CONNS = [
    {"service_connection_id": "conn-superpos", "target_login": "Superpos-AI",
     "broker_compatible": True},
    {"service_connection_id": "conn-address", "target_login": "address-so",
     "broker_compatible": True},
]


def _std_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERPOS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://hive.example")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive-1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "api-tok")
    monkeypatch.delenv("SUPERPOS_GITHUB_CONNECTION_ID", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


async def test_resolve_connection_id_matches_owner(tmp_path, monkeypatch):
    _std_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(_TWO_CONNS))
    assert await ga._resolve_connection_id("address-so") == "conn-address"
    assert await ga._resolve_connection_id("Superpos-AI") == "conn-superpos"


async def test_resolve_connection_id_owner_match_case_insensitive(tmp_path, monkeypatch):
    _std_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(_TWO_CONNS))
    # Owner casing differs from the persona target_login — must still match.
    assert await ga._resolve_connection_id("ADDRESS-SO") == "conn-address"
    assert await ga._resolve_connection_id("superpos-ai") == "conn-superpos"


async def test_resolve_connection_id_override_wins_over_owner(tmp_path, monkeypatch):
    _std_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SUPERPOS_GITHUB_CONNECTION_ID", "conn-pinned")
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(_TWO_CONNS))
    # Override short-circuits before any persona lookup, even with a valid owner.
    assert await ga._resolve_connection_id("address-so") == "conn-pinned"


async def test_resolve_connection_id_single_connection_default(tmp_path, monkeypatch):
    _std_env(monkeypatch, tmp_path)
    one = [{"service_connection_id": "conn-solo", "target_login": "solo-org",
            "broker_compatible": True}]
    monkeypatch.setattr(
        ga, "SuperposClient", _persona_client(one, default_id="conn-solo")
    )
    # No owner → default_connection_id (single-connection agents, unchanged).
    assert await ga._resolve_connection_id(None) == "conn-solo"
    # Even an owner that doesn't match is fine with a single connection.
    assert await ga._resolve_connection_id("whoever") == "conn-solo"


async def test_resolve_connection_id_ambiguous_no_match_fails_clear(tmp_path, monkeypatch):
    _std_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(_TWO_CONNS))
    # ≥2 connections, owner matches none, no override/default → refuse to guess.
    with pytest.raises(ga._AmbiguousConnection):
        await ga._resolve_connection_id("unrelated-org")


async def test_resolve_connection_id_ambiguous_no_owner_fails_clear(tmp_path, monkeypatch):
    _std_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(_TWO_CONNS))
    # ≥2 connections, no owner, no override/default → refuse to guess.
    with pytest.raises(ga._AmbiguousConnection):
        await ga._resolve_connection_id(None)


async def test_resolve_connection_id_skips_pat_for_matching_owner(tmp_path, monkeypatch):
    # A persona holding BOTH a PAT and a github_app connection for the SAME
    # owner must resolve to the github_app (broker-compatible) one — the broker
    # cannot mint an installation token from a PAT, so its id must never reach
    # the mint endpoint even when its target_login matches the repo owner.
    _std_env(monkeypatch, tmp_path)
    mixed = [
        {"service_connection_id": "pat-superpos", "target_login": "Superpos-AI",
         "broker_compatible": False},
        {"service_connection_id": "app-superpos", "target_login": "Superpos-AI",
         "broker_compatible": True},
    ]
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(mixed))
    # Owner match: the PAT is filtered out, leaving one App connection — no
    # ambiguity, and definitely not the PAT's id.
    assert await ga._resolve_connection_id("Superpos-AI") == "app-superpos"


async def test_resolve_connection_id_pat_only_falls_through(tmp_path, monkeypatch):
    # A persona whose only owner-matching connection is a PAT yields no
    # broker-compatible connection → _resolve_connection_id returns None so the
    # caller falls through to the static GITHUB_TOKEN path (never mints the PAT).
    _std_env(monkeypatch, tmp_path)
    pat_only = [
        {"service_connection_id": "pat-superpos", "target_login": "Superpos-AI",
         "broker_compatible": False},
    ]
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(pat_only))
    assert await ga._resolve_connection_id("Superpos-AI") is None


async def test_connections_from_persona_drops_pat_default():
    # A default_connection_id pointing at a PAT (broker_compatible=False)
    # connection must be dropped: with no owner, _resolve_connection_id would
    # otherwise hand that PAT id straight to the installation-token mint
    # endpoint, which cannot mint from a PAT. Mirror
    # _resolve_app_connection_from_persona, which only honours a default that
    # names a broker-compatible connection.
    mixed = [
        {"service_connection_id": "pat-superpos", "target_login": "Superpos-AI",
         "broker_compatible": False},
        {"service_connection_id": "app-address", "target_login": "address-so",
         "broker_compatible": True},
    ]
    client = _persona_client(mixed, default_id="pat-superpos")()
    broker, default_id = await ga._connections_from_persona(client)
    assert [c["service_connection_id"] for c in broker] == ["app-address"]
    assert default_id is None


async def test_connections_from_persona_keeps_broker_default():
    # The counterpart: a default naming a broker-compatible connection is kept.
    conns = [
        {"service_connection_id": "app-solo", "target_login": "solo-org",
         "broker_compatible": True},
    ]
    client = _persona_client(conns, default_id="app-solo")()
    broker, default_id = await ga._connections_from_persona(client)
    assert default_id == "app-solo"


# ── credential get end-to-end: mints for the OWNING connection ──────────


class _OwnerAwareMintClient:
    """Records every mint_github_token call so tests can assert the RIGHT
    connection id was minted per owner."""

    minted: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def get_persona(self):
        return {
            "github": {
                "connections": _TWO_CONNS,
                "default_connection_id": None,
            }
        }

    async def mint_github_token(self, conn_id):
        type(self).minted.append(conn_id)
        return {"token": f"tok_{conn_id}", "expires_at": _iso(3600)}

    async def close(self):
        pass


async def test_mint_token_owner_resolves_and_mints_correct_connection(tmp_path, monkeypatch):
    _std_env(monkeypatch, tmp_path)
    _OwnerAwareMintClient.minted = []
    monkeypatch.setattr(ga, "SuperposClient", _OwnerAwareMintClient)

    tok = await ga._mint_token("address-so")
    assert tok == "tok_conn-address"
    assert _OwnerAwareMintClient.minted == ["conn-address"]


async def test_mint_token_cache_distinct_per_owner(tmp_path, monkeypatch):
    _std_env(monkeypatch, tmp_path)
    _OwnerAwareMintClient.minted = []
    monkeypatch.setattr(ga, "SuperposClient", _OwnerAwareMintClient)

    # Two sequential requests for different owners must mint two DIFFERENT
    # connection ids — no stale cross-org token reuse.
    tok_a = await ga._mint_token("Superpos-AI")
    tok_b = await ga._mint_token("address-so")
    assert tok_a == "tok_conn-superpos"
    assert tok_b == "tok_conn-address"
    assert _OwnerAwareMintClient.minted == ["conn-superpos", "conn-address"]
    # A repeat request for the first owner reuses the cache (no re-mint) only if
    # its token is still cached under that connection id.
    cached = json.loads(ga._token_cache_path().read_text())
    assert cached["connection_id"] == "conn-address"


def test_credential_get_fails_clear_on_ambiguous(monkeypatch, tmp_path):
    _std_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(_TWO_CONNS))
    # Owner matches none of two connections → credential request fails (rc=1)
    # and emits no credential rather than minting for the wrong org.
    rc, out = _run_credential(
        monkeypatch, "protocol=https\nhost=github.com\npath=nobody/repo.git\n\n"
    )
    assert rc == 1
    assert out == ""


# ── ownerless fast path must not reuse an ambiguous bootstrap cache ──────


def _seed_connection_cache(record):
    ga._write_json_private(ga._connection_cache_path(), record)


def _seed_token_cache(conn_id, token):
    ga._write_json_private(
        ga._token_cache_path(),
        {"token": token, "expires_at": _iso(3600), "connection_id": conn_id},
    )


class _RaiseOnMintClient(_persona_client(_TWO_CONNS)):  # type: ignore[misc]
    """Persona holds two App connections; minting must never be reached on the
    ownerless fast path (that is the whole point of the fast path)."""

    async def mint_github_token(self, conn_id):  # pragma: no cover - guard
        raise AssertionError(f"unexpected mint for {conn_id!r}")


async def test_mint_token_ownerless_refuses_ambiguous_bootstrap_cache(
    tmp_path, monkeypatch
):
    _std_env(monkeypatch, tmp_path)
    # setup writes app_conns[0] with ambiguous=True when it bootstraps gh from
    # two-or-more App connections, and mints a token for it.  An ownerless
    # credential request (older git / useHttpPath off) must NOT reuse that
    # arbitrary connection — it would authenticate to the wrong org.  With two
    # persona connections and no default, resolution must fail clear instead.
    _seed_connection_cache(
        {"id": "conn-superpos", "name": "gh", "owner_agnostic": False}
    )
    _seed_token_cache("conn-superpos", "boot_tok")
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(_TWO_CONNS))

    with pytest.raises(ga._AmbiguousConnection):
        await ga._mint_token(None)


async def test_mint_token_ownerless_refuses_legacy_unmarked_cache(
    tmp_path, monkeypatch
):
    _std_env(monkeypatch, tmp_path)
    # A connection.json written by a prior release has no owner_agnostic marker.
    # Treating a missing marker as "safe" would let an ownerless request reuse
    # an arbitrary bootstrap connection minted before the marker existed.  It
    # must be refused and fall through to owner-aware resolution instead.
    _seed_connection_cache({"id": "conn-superpos", "name": "gh"})
    _seed_token_cache("conn-superpos", "legacy_tok")
    monkeypatch.setattr(ga, "SuperposClient", _persona_client(_TWO_CONNS))

    with pytest.raises(ga._AmbiguousConnection):
        await ga._mint_token(None)


async def test_mint_token_ownerless_reuses_owner_agnostic_cache(tmp_path, monkeypatch):
    _std_env(monkeypatch, tmp_path)
    # The counterpart: a cache proven owner-agnostic (a valid default or the
    # sole connection — owner_agnostic=True) is still fast-pathed, returning the
    # cached token without any network mint.
    _seed_connection_cache(
        {"id": "conn-solo", "name": "gh", "owner_agnostic": True}
    )
    _seed_token_cache("conn-solo", "solo_tok")
    monkeypatch.setattr(ga, "SuperposClient", _RaiseOnMintClient)

    assert await ga._mint_token(None) == "solo_tok"


async def test_resolve_app_connection_marks_multi_cache_not_owner_agnostic(
    tmp_path, monkeypatch
):
    _std_env(monkeypatch, tmp_path)

    class _TwoAppCatalog:
        def __init__(self, *args, **kwargs):
            pass

        async def list_github_connections(self):
            return [
                {"id": "conn-a", "name": "gh-a",
                 "metadata": {"auth_type": "github_app"}},
                {"id": "conn-b", "name": "gh-b",
                 "metadata": {"auth_type": "github_app"}},
            ]

        async def close(self):
            pass

    # Ownerless bootstrap over two App connections picks the first for gh, but
    # the persisted cache must NOT be marked owner-agnostic so the mint fast
    # path refuses to reuse it later.
    await ga._resolve_app_connection(_TwoAppCatalog())  # type: ignore[arg-type]
    cached = json.loads(ga._connection_cache_path().read_text())
    assert cached["owner_agnostic"] is False
    assert cached["id"] == "conn-a"


async def test_resolve_app_connection_marks_sole_cache_owner_agnostic(
    tmp_path, monkeypatch
):
    _std_env(monkeypatch, tmp_path)

    class _OneAppCatalog:
        def __init__(self, *args, **kwargs):
            pass

        async def list_github_connections(self):
            return [
                {"id": "conn-solo", "name": "gh-solo",
                 "metadata": {"auth_type": "github_app"}},
            ]

        async def close(self):
            pass

    # The sole broker-compatible connection is genuinely owner-agnostic — any
    # request (owner or not) resolves to it — so its cache is reusable.
    await ga._resolve_app_connection(_OneAppCatalog())  # type: ignore[arg-type]
    cached = json.loads(ga._connection_cache_path().read_text())
    assert cached["owner_agnostic"] is True
    assert cached["id"] == "conn-solo"


class _ForbiddenCatalogPersonaClient:
    """Catalog discovery is 403 (no services.read); the persona block holds
    two broker-compatible connections so resolution falls back to it."""

    def __init__(self, *args, **kwargs):
        pass

    async def list_github_connections(self):
        raise GitHubDiscoveryForbidden(403, "no services.read")

    async def get_persona(self):
        return {"github": {"connections": _TWO_CONNS, "default_connection_id": None}}

    async def close(self):
        pass


async def test_resolve_app_connection_persona_owner_match_not_owner_agnostic(
    tmp_path, monkeypatch
):
    _std_env(monkeypatch, tmp_path)
    # Catalog 403 → persona fallback resolves the owner-matched connection out
    # of two. That id is owner-specific: it must be cached NON-owner-agnostic so
    # a later ownerless request does not reuse the wrong org's connection.
    record = await ga._resolve_app_connection(
        _ForbiddenCatalogPersonaClient(), owner="address-so"  # type: ignore[arg-type]
    )
    assert record["id"] == "conn-address"
    cached = json.loads(ga._connection_cache_path().read_text())
    assert cached["owner_agnostic"] is False
    assert cached["id"] == "conn-address"
    # And the ownerless fast path must refuse the owner-matched cache.
    assert ga._cached_connection_id() is None


# ── persona unavailable: owner request must NOT fall back to catalog[0] ──


class _NoPersonaTwoAppCatalogClient:
    """Persona is unavailable (``get_persona`` → None) but the raw catalog
    holds two ``github_app`` connections.  Records every mint so a test can
    assert the WRONG-org connection is never minted."""

    minted: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def get_persona(self):
        return None

    async def list_github_connections(self):
        return [
            {"id": "conn-a", "metadata": {"auth_type": "github_app"}},
            {"id": "conn-b", "metadata": {"auth_type": "github_app"}},
        ]

    async def mint_github_token(self, conn_id):
        type(self).minted.append(conn_id)
        return {"token": f"tok_{conn_id}", "expires_at": _iso(3600)}

    async def close(self):
        pass


async def test_mint_token_owner_fails_clear_when_persona_unavailable(
    tmp_path, monkeypatch
):
    _std_env(monkeypatch, tmp_path)
    _NoPersonaTwoAppCatalogClient.minted = []
    monkeypatch.setattr(ga, "SuperposClient", _NoPersonaTwoAppCatalogClient)

    # Persona resolution yields no connection id, so _mint_token falls back to
    # catalog discovery.  With an owner and two App connections the catalog
    # cannot prove which installation owns the repo → refuse to guess rather
    # than minting for the first (wrong) connection.
    with pytest.raises(ga._AmbiguousConnection):
        await ga._mint_token("org-b")
    assert _NoPersonaTwoAppCatalogClient.minted == []


# ── setup enables useHttpPath ───────────────────────────────────────────


def test_configure_helper_sets_use_http_path(monkeypatch):
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(ga.subprocess, "run", fake_run)
    ga._configure_app_credential_helper()

    joined = [" ".join(c) for c in calls]
    # useHttpPath is set to true so git forwards the repo path to the helper.
    assert any(
        "credential.https://github.com.useHttpPath true" in j for j in joined
    ), joined


# ── gh owner-aware token: --owner / --repo ──────────────────────────────


@pytest.mark.parametrize(
    "repo,expected",
    [
        ("git@github.com:acme/widgets.git", "acme"),
        ("https://github.com/acme/widgets.git", "acme"),
        ("https://github.com/acme/widgets", "acme"),
        ("ssh://git@github.com/acme/widgets", "acme"),
        ("acme/widgets", "acme"),
        ("acme/widgets.git", "acme"),
        (None, None),
        ("", None),
    ],
)
def test_owner_from_repo_arg(repo, expected):
    assert ga._owner_from_repo_arg(repo) == expected


def test_cmd_token_static_ignores_owner(monkeypatch, capsys):
    # A static GITHUB_TOKEN always wins and ignores owner resolution entirely.
    monkeypatch.setenv("GITHUB_TOKEN", "STATIC")

    async def fake_mint(owner=None):  # pragma: no cover - must not be called
        raise AssertionError("should not mint when GITHUB_TOKEN is set")

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    rc = ga.cmd_token(owner="acme")
    assert rc == 0
    assert capsys.readouterr().out == "STATIC"


def test_cmd_token_owner_flows_to_mint(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SUPERPOS_GITHUB_REPO_OWNER", raising=False)
    seen = {}

    async def fake_mint(owner=None):
        seen["owner"] = owner
        return f"tok-for-{owner}"

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    rc = ga.cmd_token(owner="address-so")
    assert rc == 0
    assert seen["owner"] == "address-so"
    assert capsys.readouterr().out == "tok-for-address-so"


def test_cmd_token_repo_url_owner_flows_to_mint(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SUPERPOS_GITHUB_REPO_OWNER", raising=False)
    seen = {}

    async def fake_mint(owner=None):
        seen["owner"] = owner
        return "tok"

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    # A git remote URL is accepted and the owner parsed out.
    rc = ga.cmd_token(repo="git@github.com:Superpos-AI/x.git")
    assert rc == 0
    assert seen["owner"] == "Superpos-AI"


def test_cmd_token_owner_beats_repo_and_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("SUPERPOS_GITHUB_REPO_OWNER", "env-org")
    seen = {}

    async def fake_mint(owner=None):
        seen["owner"] = owner
        return "tok"

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    # Explicit --owner wins over --repo and over the env hint.
    ga.cmd_token(owner="explicit-org", repo="git@github.com:repo-org/x.git")
    assert seen["owner"] == "explicit-org"


def test_cmd_token_repo_beats_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("SUPERPOS_GITHUB_REPO_OWNER", "env-org")
    seen = {}

    async def fake_mint(owner=None):
        seen["owner"] = owner
        return "tok"

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    # --repo (parsed owner) wins over the env hint when --owner is absent.
    ga.cmd_token(repo="repo-org/x")
    assert seen["owner"] == "repo-org"


def test_cmd_token_ambiguous_fails_clear(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    async def fake_mint(owner=None):
        raise ga._AmbiguousConnection(owner)

    monkeypatch.setattr(ga, "_mint_token", fake_mint)
    rc = ga.cmd_token(owner="nobody")
    assert rc == 1
    err = capsys.readouterr().err
    assert "--owner/--repo" in err


def test_main_token_parses_owner_and_repo(monkeypatch):
    seen = {}

    def fake_cmd_token(owner=None, repo=None):
        seen["owner"] = owner
        seen["repo"] = repo
        return 0

    monkeypatch.setattr(ga, "cmd_token", fake_cmd_token)
    rc = ga.main(["token", "--owner", "acme", "--repo", "acme/widgets"])
    assert rc == 0
    assert seen == {"owner": "acme", "repo": "acme/widgets"}


def test_main_token_no_args_still_works(monkeypatch):
    seen = {}

    def fake_cmd_token(owner=None, repo=None):
        seen["owner"] = owner
        seen["repo"] = repo
        return 0

    monkeypatch.setattr(ga, "cmd_token", fake_cmd_token)
    rc = ga.main(["token"])
    assert rc == 0
    assert seen == {"owner": None, "repo": None}
