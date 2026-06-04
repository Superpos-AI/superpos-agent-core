"""GitHub credential bootstrap shared by every Superpos LLM-backed agent.

Two GitHub access paths coexist, and this module wires the *direct git/gh*
one (Path A).  The other (Path B — GitHub REST through Superpos's credentialed
proxy) is the ``superpos-github`` module and needs no local credentials.

Token precedence — static ``GITHUB_TOKEN`` always wins (the canonical path,
and the *only* path for PAT-backed orgs, since the broker refuses to hand back
personal access tokens):

  1. ``GITHUB_TOKEN`` set → use it verbatim.  ``setup`` runs the classic
     ``gh auth login --with-token`` + ``gh auth setup-git``.  Nothing expires.
  2. else a ``github_app`` service connection is discoverable → register a git
     credential helper that mints a short-lived installation token from the
     Superpos broker *on demand* (so ``git`` always sees a fresh, ~1h token
     without a long-lived secret living in the container).  ``gh`` does *not*
     consult git's credential helper, so ``setup`` additionally logs ``gh`` in
     with a freshly minted token; long-lived sessions re-mint via ``token``.
  3. else → no direct GitHub auth; the agent can still reach GitHub through the
     Superpos proxy (``superpos-github``).

Subcommands (all invoked from the agent entrypoint or by git itself):

  setup       Decide the path above and configure git + gh accordingly.
  credential  git credential helper protocol (``get``/``store``/``erase``).
  token       Print a fresh installation token to stdout, e.g.
              ``GH_TOKEN="$(... token)" gh pr create ...``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import BaseConfig
from .superpos_client import SuperposClient

log = logging.getLogger(__name__)

# Re-mint this many seconds before the broker-reported expiry to absorb clock
# skew and long-running git operations that straddle the boundary.
_EXPIRY_SKEW_SECONDS = 300

# git's credential protocol asks for ``x-access-token`` as the username when
# authenticating with a GitHub App installation token.
_GIT_USERNAME = "x-access-token"

_GITHUB_HOST = "github.com"


# ── env / state plumbing ──────────────────────────────────────────────


def _config_from_env() -> BaseConfig:
    base_url = os.environ.get("SUPERPOS_BASE_URL", "").rstrip("/")
    hive_id = os.environ.get("SUPERPOS_HIVE_ID", "")
    token = os.environ.get("SUPERPOS_API_TOKEN", "")
    if not (base_url and hive_id and token):
        raise SystemExit(
            "github_auth: SUPERPOS_BASE_URL, SUPERPOS_HIVE_ID, and "
            "SUPERPOS_API_TOKEN must be set."
        )
    return BaseConfig(
        superpos_base_url=base_url,
        superpos_hive_id=hive_id,
        superpos_agent_id=os.environ.get("SUPERPOS_AGENT_ID", ""),
        superpos_api_token=token,
        superpos_refresh_token=os.environ.get("SUPERPOS_REFRESH_TOKEN", ""),
    )


def _state_dir() -> Path:
    """Where minted-token and resolved-connection caches live.

    Defaults under ``$HOME`` so the cache survives across the short-lived
    credential-helper processes git spawns, but stays inside the container.
    """
    root = os.environ.get("SUPERPOS_STATE_DIR") or os.path.join(
        os.environ.get("HOME", "/tmp"), ".superpos"
    )
    path = Path(root) / "github"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _connection_cache_path() -> Path:
    return _state_dir() / "connection.json"


def _token_cache_path() -> Path:
    # Single installation-wide token cache: the broker mints installation
    # tokens that are not scoped to a single repo, so one cached token serves
    # every repository the App installation can reach.
    return _state_dir() / "token.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json_private(path: Path, data: dict[str, Any]) -> None:
    # Tokens are secrets — write 0600 and never log the value.
    path.write_text(json.dumps(data))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _is_fresh(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    remaining = (exp - datetime.now(timezone.utc)).total_seconds()
    return remaining > _EXPIRY_SKEW_SECONDS


# ── connection discovery ──────────────────────────────────────────────


async def _resolve_app_connection(client: SuperposClient) -> dict[str, Any] | None:
    """Find an active ``github_app`` connection and cache its id/name.

    PAT (``auth_type=token``) connections are skipped here: the broker can't
    mint from them, so the direct git/gh path falls through to the static
    ``GITHUB_TOKEN`` rule.  They remain usable via the proxy (Path B).
    """
    connections = await client.list_github_connections()
    app_conns = [
        c
        for c in connections
        if (c.get("metadata") or {}).get("auth_type") == "github_app"
    ]
    if not app_conns:
        return None
    conn = app_conns[0]
    if len(app_conns) > 1:
        log.warning(
            "Multiple github_app connections found; using %r. Set "
            "SUPERPOS_GITHUB_CONNECTION_ID to pin a specific one.",
            conn.get("name"),
        )
    record = {"id": conn.get("id"), "name": conn.get("name")}
    _write_json_private(_connection_cache_path(), record)
    return record


def _cached_connection_id() -> str | None:
    # Explicit override beats discovery; otherwise reuse setup's resolution so
    # the credential helper never has to hit the catalog on the hot path.
    override = os.environ.get("SUPERPOS_GITHUB_CONNECTION_ID")
    if override:
        return override
    cached = _read_json(_connection_cache_path())
    return cached.get("id") if cached else None


# ── token minting (cached) ────────────────────────────────────────────


async def _mint_token() -> str | None:
    """Return a fresh installation token, minting via the broker if needed.

    The broker issues an installation-wide token (it does not honour per-repo
    scoping), so a single cached token is reused for every repository.
    """
    cache_path = _token_cache_path()
    cached = _read_json(cache_path)
    if cached and _is_fresh(cached.get("expires_at")):
        return cached.get("token")

    client = SuperposClient(_config_from_env())
    try:
        conn_id = _cached_connection_id()
        if not conn_id:
            conn = await _resolve_app_connection(client)
            if not conn:
                return None
            conn_id = conn["id"]
        result = await client.mint_github_token(conn_id)
    finally:
        await client.close()

    token = result.get("token")
    if not token:
        return None
    _write_json_private(
        cache_path, {"token": token, "expires_at": result.get("expires_at")}
    )
    return token


# ── subcommands ───────────────────────────────────────────────────────


def _git_config(*args: str) -> None:
    subprocess.run(["git", "config", "--global", *args], check=False)


def _gh_login_with_token(token: str) -> None:
    """Authenticate ``gh`` with a token via ``gh auth login --with-token``.

    ``gh`` ignores git's credential helper for its own API calls (it reads
    ``GH_TOKEN``/``GITHUB_TOKEN`` or credentials stored by ``gh auth login``),
    so the brokered git helper alone leaves ``gh pr create``/``gh api``
    unauthenticated.  We do *not* run ``gh auth setup-git`` here — git is
    already wired to our on-demand credential helper and must not be handed
    back to ``gh``'s static credentials.
    """
    subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=token,
        text=True,
        check=False,
    )


def _configure_app_credential_helper() -> None:
    """Point git at this module's credential helper for github.com only.

    The broker mints installation-wide tokens, so the helper does not vary by
    repository.  We replace (not append) any existing helper for the host to
    avoid stacking a stale one from a previous boot.
    """
    helper = f"!{sys.executable} -m superpos_agent_core.github_auth credential"
    key = f"credential.https://{_GITHUB_HOST}.helper"
    # Clear then set, so re-running setup is idempotent.
    subprocess.run(
        ["git", "config", "--global", "--unset-all", key],
        check=False,
    )
    _git_config("--add", key, helper)
    # Tokens are installation-wide; drop any stale useHttpPath from a previous
    # boot so git does not needlessly vary credential lookups by repo path.
    subprocess.run(
        [
            "git", "config", "--global", "--unset-all",
            f"credential.https://{_GITHUB_HOST}.useHttpPath",
        ],
        check=False,
    )


def cmd_setup() -> int:
    static = os.environ.get("GITHUB_TOKEN")
    if static:
        # Canonical path — unchanged behaviour, nothing expires.
        subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=static,
            text=True,
            check=False,
        )
        subprocess.run(["gh", "auth", "setup-git"], check=False)
        log.info("GitHub: authenticated gh/git with static GITHUB_TOKEN.")
        return 0

    try:
        config = _config_from_env()
    except SystemExit:
        log.info("GitHub: no GITHUB_TOKEN and Superpos env incomplete — skipping.")
        return 0

    async def _resolve() -> dict[str, Any] | None:
        client = SuperposClient(config)
        try:
            return await _resolve_app_connection(client)
        finally:
            await client.close()

    conn = asyncio.run(_resolve())

    if not conn:
        log.info(
            "GitHub: no GITHUB_TOKEN and no github_app connection — direct "
            "git/gh disabled (proxy access via superpos-github still works)."
        )
        return 0

    _configure_app_credential_helper()

    # git now mints on demand via the helper, but gh won't — log it in with a
    # freshly minted token so direct ``gh`` calls work right after setup.
    token = asyncio.run(_mint_token(None))
    if token:
        _gh_login_with_token(token)
        log.info(
            "GitHub: git mints short-lived App tokens on demand via connection "
            "%r; gh authenticated with a freshly minted token.",
            conn.get("name"),
        )
    else:
        log.warning(
            "GitHub: configured git credential helper for connection %r, but "
            "could not mint a token to authenticate gh; gh calls may fail until "
            "a token is available.",
            conn.get("name"),
        )
    return 0


def cmd_credential(action: str) -> int:
    """git credential helper.  Only ``get`` does work; others are no-ops."""
    if action != "get":
        # store / erase: nothing to persist or revoke (tokens are ephemeral).
        return 0

    # git feeds key=value lines on stdin, terminated by a blank line.
    attrs: dict[str, str] = {}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        if "=" in line:
            key, _, value = line.partition("=")
            attrs[key] = value

    if attrs.get("host") != _GITHUB_HOST:
        return 0  # not ours — let git try other helpers

    # The minted token is installation-wide, so any ``path`` git supplies is
    # irrelevant — one token serves every repo.
    token = asyncio.run(_mint_token())
    if not token:
        return 0  # fall through to any other helper / fail naturally

    sys.stdout.write(
        f"protocol=https\nhost={_GITHUB_HOST}\n"
        f"username={_GIT_USERNAME}\npassword={token}\n"
    )
    return 0


def cmd_token() -> int:
    """Print a fresh token (static or broker-minted) for ``gh`` invocations."""
    static = os.environ.get("GITHUB_TOKEN")
    if static:
        sys.stdout.write(static)
        return 0
    token = asyncio.run(_mint_token())
    if not token:
        print("github_auth: no GitHub credential available.", file=sys.stderr)
        return 1
    sys.stdout.write(token)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="superpos-github-auth")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Configure git/gh GitHub auth at startup.")

    cred = sub.add_parser("credential", help="git credential helper protocol.")
    cred.add_argument("action", choices=["get", "store", "erase"])

    sub.add_parser("token", help="Print a fresh GitHub token to stdout.")

    args = parser.parse_args(argv)
    if args.command == "setup":
        return cmd_setup()
    if args.command == "credential":
        return cmd_credential(args.action)
    if args.command == "token":
        return cmd_token()
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
