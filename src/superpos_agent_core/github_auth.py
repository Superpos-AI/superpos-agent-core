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
from .superpos_client import GitHubDiscoveryForbidden, SuperposClient

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


def _owner_from_path(path: str | None) -> str | None:
    """Extract the repo *owner* from git's ``path=owner/repo.git`` line.

    Git forwards the HTTP path only when ``credential.useHttpPath=true`` (which
    ``setup`` configures).  The owner is the first path segment; a trailing
    ``.git`` on the segment is stripped.  Returns ``None`` when there is no
    usable path (older git, or the value is empty), so callers fall back to the
    non-owner-aware precedence.
    """
    if not path:
        return None
    first = path.strip("/").split("/", 1)[0]
    if first.endswith(".git"):
        first = first[: -len(".git")]
    return first or None


def _owner_from_repo_arg(repo: str | None) -> str | None:
    """Extract the repo *owner* from an ``owner/repo`` slug or a git remote URL.

    Accepts what ``git config --get remote.origin.url`` prints as well as a bare
    ``owner/repo``:

      * ``git@github.com:acme/widgets.git`` → ``acme``
      * ``https://github.com/acme/widgets.git`` → ``acme``
      * ``ssh://git@github.com/acme/widgets`` → ``acme``
      * ``acme/widgets`` → ``acme``

    Returns ``None`` when no owner can be parsed, so callers fall back to the
    non-owner-aware precedence.
    """
    if not repo:
        return None
    val = repo.strip()
    if not val:
        return None
    # scp-style ``git@host:owner/repo`` — the owner follows the ``:``.
    if "@" in val and ":" in val and "://" not in val:
        val = val.rsplit(":", 1)[-1]
    # URL form ``scheme://host/owner/repo`` — drop scheme + host, keep the path.
    elif "://" in val:
        after_scheme = val.split("://", 1)[1]
        val = after_scheme.split("/", 1)[1] if "/" in after_scheme else ""
    # Now ``val`` looks like ``owner/repo[.git]`` (or just ``owner``).
    return _owner_from_path(val)


async def _connections_from_persona(
    client: SuperposClient,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(connections, default_connection_id)`` from the persona block.

    The persona ``github`` block (rendered server-side by ``GitHubVersionService``)
    carries per-connection ``target_login`` — the org/user the App installation
    is bound to — which the raw services catalog does not.  This is the source
    of truth for owner-aware resolution and needs only the free
    ``services.read`` scope the persona render already relies on.

    Only broker-compatible (``github_app``) connections are returned: the
    broker mints installation tokens and cannot do so from a PAT
    (``broker_compatible=False``) connection, so a PAT connection must never be
    handed to the mint endpoint — even when its ``target_login`` matches the
    repo owner.  This mirrors :func:`_resolve_app_connection_from_persona`,
    which filters the same way before matching owner/default.

    Returns ``([], None)`` on any failure so callers fall back to catalog
    discovery or the static ``GITHUB_TOKEN`` path.
    """
    try:
        persona = await client.get_persona()
    except Exception:  # pragma: no cover - get_persona already swallows errors
        return [], None
    if not isinstance(persona, dict):
        return [], None
    block = persona.get("github")
    if not isinstance(block, dict):
        return [], None
    conns = block.get("connections")
    if not isinstance(conns, list):
        conns = []
    broker = [c for c in conns if isinstance(c, dict) and c.get("broker_compatible")]
    default_id = block.get("default_connection_id")
    if not isinstance(default_id, str):
        default_id = None
    elif default_id not in {_conn_id(c) for c in broker}:
        # A default pointing at a PAT (broker_compatible=False) connection must
        # never be handed to the mint endpoint, which cannot mint from a PAT.
        # Mirror _resolve_app_connection_from_persona, which only honours the
        # default when it names a broker-compatible connection.
        default_id = None
    return broker, default_id


def _conn_id(conn: dict[str, Any]) -> str | None:
    """A connection's broker id — persona uses ``service_connection_id``, the
    raw catalog uses ``id``.  Accept either."""
    val = conn.get("service_connection_id") or conn.get("id")
    return val if isinstance(val, str) else None


def _match_owner(
    conns: list[dict[str, Any]], owner: str
) -> list[dict[str, Any]]:
    """Connections whose ``target_login`` equals ``owner`` (case-insensitive)."""
    owner_lc = owner.casefold()
    return [
        c
        for c in conns
        if isinstance(c.get("target_login"), str)
        and c["target_login"].casefold() == owner_lc
    ]


class _AmbiguousConnection(Exception):
    """Raised when a repo owner cannot be resolved to a single connection and
    there is no override/default to fall back on — we refuse to guess."""


async def _resolve_connection_id(owner: str | None) -> str | None:
    """Resolve the ``github_app`` connection id to mint for.

    Precedence:
      1. ``SUPERPOS_GITHUB_CONNECTION_ID`` env override (explicit; always wins).
      2. **owner match** against the persona block's ``connections[].target_login``
         (the owner-aware path — new).
      3. ``default_connection_id`` from the persona block (single-connection
         agents; unchanged behaviour).
      4. setup's cached single-connection resolution (offline fallback).

    Raises :class:`_AmbiguousConnection` when there are two or more connections,
    an owner was supplied, and none matches it (and no override/default) — a
    clear failure beats silently minting for the wrong installation.  Returns
    ``None`` only when there is genuinely no ``github_app`` connection to use.
    """
    override = os.environ.get("SUPERPOS_GITHUB_CONNECTION_ID")
    if override:
        return override

    client = SuperposClient(_config_from_env())
    try:
        conns, default_id = await _connections_from_persona(client)
    finally:
        await client.close()

    if conns:
        if owner:
            matches = _match_owner(conns, owner)
            if len(matches) == 1:
                return _conn_id(matches[0])
            if len(matches) > 1:
                # Two connections on the same org is a real (if rare) config;
                # without a finer signal we cannot pick — fail clear.
                logins = sorted(
                    c.get("target_login", "?") for c in conns
                )
                log.error(
                    "Repo owner %r matches multiple GitHub connections %r; set "
                    "SUPERPOS_GITHUB_CONNECTION_ID to disambiguate.",
                    owner, logins,
                )
                raise _AmbiguousConnection(owner)
            # owner supplied but matched nothing.
            if len(conns) == 1:
                # Only one connection anyway — no ambiguity, use it.
                return _conn_id(conns[0])
            logins = sorted(c.get("target_login", "?") for c in conns)
            log.error(
                "No GitHub connection owns repo owner %r (available "
                "target_login: %r); set SUPERPOS_GITHUB_CONNECTION_ID to pin "
                "one.",
                owner, logins,
            )
            raise _AmbiguousConnection(owner)
        # No owner (git didn't send a path / older git): honour the default or,
        # if unambiguous, the sole connection.
        if default_id:
            return default_id
        if len(conns) == 1:
            return _conn_id(conns[0])
        logins = sorted(c.get("target_login", "?") for c in conns)
        log.error(
            "Multiple GitHub connections %r and no repo owner to resolve by; "
            "set SUPERPOS_GITHUB_CONNECTION_ID to pin one.",
            logins,
        )
        raise _AmbiguousConnection(None)

    if default_id:
        return default_id

    # Persona had no usable github block — fall back to catalog discovery
    # (setup's cached resolution, or a fresh single-connection lookup).
    return None


async def _resolve_app_connection(
    client: SuperposClient, owner: str | None = None
) -> dict[str, Any] | None:
    """Find an active ``github_app`` connection and cache its id/name.

    Used by ``setup`` (which has no repo owner yet) to decide whether the App
    path is available at all and to prime the connection cache.  PAT
    (``auth_type=token``) connections are skipped here: the broker can't mint
    from them, so the direct git/gh path falls through to the static
    ``GITHUB_TOKEN`` rule.  They remain usable via the proxy (Path B).

    A ``GitHubDiscoveryForbidden`` (HTTP 401/403, typically missing
    ``services.read``) is treated the same as "no connection exists": we
    return ``None`` and the caller falls through to the static
    ``GITHUB_TOKEN`` rule or the proxy.  Logged at debug level so the
    permission problem is not silently conflated with an empty catalog.

    ``owner`` is the repo owner of an owner-specific credential request.  The
    raw catalog has no ``target_login`` to match on, so when an owner is
    supplied and discovery finds two or more App connections we cannot prove
    which one owns the repo — raise :class:`_AmbiguousConnection` rather than
    silently minting for ``app_conns[0]`` (the wrong installation).  With no
    owner (setup's bootstrap) picking the first is fine: the per-repo helper
    still resolves the owning connection for each git operation.
    """
    try:
        connections = await client.list_github_connections()
    except GitHubDiscoveryForbidden as exc:
        # The ``services.read``-gated catalog is denied.  Before giving up, fall
        # back to the persona ``github`` block, which is scoped by the
        # per-connection ``services:{id}`` permission and lists
        # broker-compatible connections directly — so the broker path still
        # works without ``services.read``.  When an ``owner`` is supplied the
        # fallback stays owner-aware (it may raise :class:`_AmbiguousConnection`
        # rather than mint for the wrong installation).
        record = await _resolve_app_connection_from_persona(client, owner)
        if record:
            _write_json_private(_connection_cache_path(), record)
            return record
        log.debug(
            "GitHub discovery denied (HTTP %d) and the persona github block "
            "yielded no broker-compatible connection; falling back to static "
            "GITHUB_TOKEN: %s",
            exc.status_code,
            exc,
        )
        return None
    app_conns = [
        c
        for c in connections
        if (c.get("metadata") or {}).get("auth_type") == "github_app"
    ]
    if not app_conns:
        return None
    conn = app_conns[0]
    if len(app_conns) > 1:
        if owner is not None:
            # Owner-specific request but the catalog can't tell us which
            # installation owns the repo; guessing risks a wrong-org token.
            log.error(
                "Repo owner %r could not be resolved to a single GitHub "
                "connection (persona unavailable) and catalog discovery found "
                "%d github_app connections; set SUPERPOS_GITHUB_CONNECTION_ID "
                "to disambiguate.",
                owner,
                len(app_conns),
            )
            raise _AmbiguousConnection(owner)
        log.info(
            "Multiple github_app connections found; the credential helper "
            "resolves the owning one per repo. Caching %r for gh bootstrap.",
            conn.get("name"),
        )
    record = {"id": conn.get("id"), "name": conn.get("name")}
    _write_json_private(_connection_cache_path(), record)
    return record


async def _resolve_app_connection_from_persona(
    client: SuperposClient, owner: str | None = None
) -> dict[str, Any] | None:
    """Resolve a broker-compatible connection from the persona ``github`` block.

    Used as the fallback when the ``services.read`` catalog is denied.  The
    block is scoped by the per-connection ``services:{id}`` permission, so it
    surfaces connections the agent may use even without catalog read.  Only
    broker-compatible (``github_app``) connections can mint installation
    tokens, so PAT-backed ones are skipped here.

    When ``owner`` is supplied and there is more than one broker-compatible
    connection, the owner is matched against ``target_login`` first (the
    owner-aware path).  A supplied owner that matches none of two-or-more
    connections raises :class:`_AmbiguousConnection` rather than guessing.

    Otherwise it prefers ``default_connection_id`` when set (the backend only
    sets it when exactly one broker-compatible connection is permitted);
    failing that, the single broker-compatible connection when there's exactly
    one.  Returns ``{"id", "name"}`` or ``None``.
    """
    persona = await client.get_persona()
    if not persona:
        return None
    block = persona.get("github")
    if not isinstance(block, dict):
        return None
    conns = block.get("connections")
    if not isinstance(conns, list):
        return None
    broker = [c for c in conns if c.get("broker_compatible")]
    if not broker:
        return None

    def _record(conn: dict[str, Any]) -> dict[str, Any]:
        return {"id": conn.get("service_connection_id"), "name": conn.get("name")}

    if owner:
        matches = _match_owner(broker, owner)
        if len(matches) == 1:
            return _record(matches[0])
        if len(matches) > 1:
            logins = sorted(c.get("target_login", "?") for c in broker)
            log.error(
                "Repo owner %r matches multiple GitHub connections %r; set "
                "SUPERPOS_GITHUB_CONNECTION_ID to disambiguate.",
                owner, logins,
            )
            raise _AmbiguousConnection(owner)
        if len(broker) == 1:
            return _record(broker[0])
        logins = sorted(c.get("target_login", "?") for c in broker)
        log.error(
            "No GitHub connection owns repo owner %r (available "
            "target_login: %r); set SUPERPOS_GITHUB_CONNECTION_ID to pin one.",
            owner, logins,
        )
        raise _AmbiguousConnection(owner)

    default_id = block.get("default_connection_id")
    chosen = None
    if default_id:
        chosen = next(
            (c for c in broker if c.get("service_connection_id") == default_id),
            None,
        )
    if chosen is None and len(broker) == 1:
        chosen = broker[0]
    if chosen is None:
        log.warning(
            "Multiple broker-compatible GitHub connections in the persona "
            "block and no default; set SUPERPOS_GITHUB_CONNECTION_ID to pin "
            "one.",
        )
        return None
    return _record(chosen)


def _cached_connection_id() -> str | None:
    # Explicit override beats discovery; otherwise reuse setup's resolution so
    # the credential helper never has to hit the catalog on the hot path.
    override = os.environ.get("SUPERPOS_GITHUB_CONNECTION_ID")
    if override:
        return override
    cached = _read_json(_connection_cache_path())
    return cached.get("id") if cached else None


# ── token minting (cached) ────────────────────────────────────────────


def _cache_matches(cached: dict[str, Any] | None, conn_id: str) -> bool:
    """A cached token is reusable only when it is fresh *and* was minted for
    the connection we are about to use."""
    return bool(
        cached
        and cached.get("connection_id") == conn_id
        and _is_fresh(cached.get("expires_at"))
    )


async def _mint_for_connection(conn_id: str) -> str | None:
    """Mint (and cache) a token for a specific connection id, no resolution.

    Used by ``setup`` to give ``gh`` *a* working token when the agent holds
    several connections and there is no override/default to pick one — the
    per-repo git credential helper still resolves the owning connection.
    """
    if not conn_id:
        return None
    cache_path = _token_cache_path()
    cached = _read_json(cache_path)
    if _cache_matches(cached, conn_id):
        return cached["token"]
    client = SuperposClient(_config_from_env())
    try:
        result = await client.mint_github_token(conn_id)
    finally:
        await client.close()
    token = result.get("token")
    if not token:
        return None
    _write_json_private(
        cache_path,
        {
            "token": token,
            "expires_at": result.get("expires_at"),
            "connection_id": conn_id,
        },
    )
    return token


async def _mint_token(owner: str | None = None) -> str | None:
    """Return a fresh installation token, minting via the broker if needed.

    The broker issues an installation-wide token (it does not honour per-repo
    scoping), so a single cached token is reused for every repository the
    *selected connection* can reach — but only while it belongs to the
    connection currently selected.  With two or more ``github_app`` connections
    the selected connection depends on ``owner`` (the repo owner git is
    authenticating): a token minted for org A must never be handed to a request
    for a repo owned by org B.  The token cache is keyed by ``connection_id``,
    so a different owner resolves to a different id → cache miss → correct
    re-mint; there is no stale cross-org reuse.

    ``owner`` is the repo owner parsed from git's ``path`` line; ``None`` when
    git did not forward a path (older git / ``useHttpPath`` off), in which case
    resolution falls back to override → default → single connection.
    """
    cache_path = _token_cache_path()
    cached = _read_json(cache_path)

    # Fast offline path: an explicit override or setup's cached single-connection
    # id, with a fresh cached token already minted for it.  Owner-aware
    # resolution needs the persona block (a network call), so only take this
    # shortcut when we can decide the connection without owner information —
    # i.e. an explicit override.  With an owner present we must resolve properly.
    if owner is None:
        conn_id = _cached_connection_id()
        if conn_id and _cache_matches(cached, conn_id):
            return cached["token"]
    elif os.environ.get("SUPERPOS_GITHUB_CONNECTION_ID"):
        conn_id = os.environ["SUPERPOS_GITHUB_CONNECTION_ID"]
        if _cache_matches(cached, conn_id):
            return cached["token"]

    # Owner-aware resolution (persona block → target_login).  Raises
    # _AmbiguousConnection when it refuses to guess; the caller turns that into
    # a clean credential-request failure rather than a wrong-org token.
    conn_id = await _resolve_connection_id(owner)

    client = SuperposClient(_config_from_env())
    try:
        if not conn_id:
            # No persona/override signal — fall back to catalog discovery, which
            # primes setup's single-connection cache for the offline hot path.
            # Pass ``owner`` so discovery fails clear (rather than minting for
            # ``app_conns[0]``) when the persona contract is missing/stale and
            # the catalog holds two or more App connections.
            conn = await _resolve_app_connection(client, owner)
            if not conn:
                return None
            conn_id = conn["id"]
        # A cached token may already belong to the resolved connection.
        if _cache_matches(cached, conn_id):
            return cached["token"]
        result = await client.mint_github_token(conn_id)
    finally:
        await client.close()

    token = result.get("token")
    if not token:
        return None
    _write_json_private(
        cache_path,
        {
            "token": token,
            "expires_at": result.get("expires_at"),
            "connection_id": conn_id,
        },
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

    A single GitHub App installation is often not the only connection an agent
    holds: with two or more ``github_app`` connections the helper must know
    *which repo* git is authenticating so it can mint from the connection that
    owns it.  Git only forwards the repo path (``path=owner/repo.git``) to a
    credential helper when ``useHttpPath`` is set, so we enable it here.  We
    replace (not append) any existing helper for the host to avoid stacking a
    stale one from a previous boot.
    """
    helper = f"!{sys.executable} -m superpos_agent_core.github_auth credential"
    key = f"credential.https://{_GITHUB_HOST}.helper"
    # Clear then set, so re-running setup is idempotent.
    subprocess.run(
        ["git", "config", "--global", "--unset-all", key],
        check=False,
    )
    _git_config("--add", key, helper)
    # Forward the repo path to the helper so it can resolve the owning
    # connection (owner-aware minting).  Clear-then-set keeps setup idempotent —
    # re-running never stacks duplicate values.
    path_key = f"credential.https://{_GITHUB_HOST}.useHttpPath"
    subprocess.run(
        ["git", "config", "--global", "--unset-all", path_key],
        check=False,
    )
    _git_config(path_key, "true")


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

    override = os.environ.get("SUPERPOS_GITHUB_CONNECTION_ID")
    if override:
        # An explicit override beats catalog discovery: the broker can still
        # mint from it even when discovery is unavailable (e.g. the agent holds
        # ``services:{id}`` but lacks ``services.read``, or the catalog is down).
        # Honour it *before* discovery so setup never bails at the ``if not
        # conn`` branch when ``_resolve_app_connection`` would return ``None``.
        conn: dict[str, Any] | None = {"id": override, "name": override}
    else:
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

    # git now mints on demand via the helper (owner-aware, per repo), but gh
    # won't — log it in with a freshly minted token so direct ``gh`` calls work
    # right after setup.  gh has no repo context at setup time, so this uses the
    # bootstrap connection resolved above; the credential helper still picks the
    # owning connection for each git operation.
    try:
        token = asyncio.run(_mint_token())
    except _AmbiguousConnection:
        # ≥2 connections with no override/default: there is no single "gh"
        # identity to pick at setup.  Mint from the bootstrap connection so gh
        # has *a* working token; per-repo git auth is still owner-resolved.
        token = asyncio.run(_mint_for_connection(str(conn.get("id"))))
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

    # With ``useHttpPath=true`` git forwards ``path=owner/repo.git``; the owner
    # selects which App connection to mint from when the agent holds several.
    owner = _owner_from_path(attrs.get("path"))
    try:
        token = asyncio.run(_mint_token(owner))
    except _AmbiguousConnection:
        # We refuse to mint a wrong-org token.  Emit nothing and fail the
        # request: git surfaces a clear auth error (the resolver already logged
        # the owner and the available target_logins), which the agent can act
        # on — far better than silently authenticating as the wrong org.
        return 1
    if not token:
        return 0  # fall through to any other helper / fail naturally

    sys.stdout.write(
        f"protocol=https\nhost={_GITHUB_HOST}\n"
        f"username={_GIT_USERNAME}\npassword={token}\n"
    )
    return 0


def cmd_token(owner: str | None = None, repo: str | None = None) -> int:
    """Print a fresh token (static or broker-minted) for ``gh`` invocations.

    Unlike the git credential helper, ``gh`` does not hand us a repo path, so
    the caller must tell us which repo it is acting on for owner-aware
    resolution.  This is how ``gh`` (which uses a single static boot token and
    cannot consult git's credential helper) gets the *right* connection's token
    per operation on multi-connection agents.

    Owner precedence (highest wins):

      1. ``SUPERPOS_GITHUB_CONNECTION_ID`` env override (inside ``_mint_token``).
      2. ``--owner`` explicit argument.
      3. ``--repo`` argument (an ``owner/repo`` slug or a git remote URL — the
         owner is parsed out).
      4. ``SUPERPOS_GITHUB_REPO_OWNER`` env hint.
      5. the single-connection default / cached resolution.

    When several connections exist and none of the above resolves one, minting
    fails clear rather than handing back the wrong org's token.
    """
    static = os.environ.get("GITHUB_TOKEN")
    if static:
        sys.stdout.write(static)
        return 0
    resolved_owner = (
        owner
        or _owner_from_repo_arg(repo)
        or (os.environ.get("SUPERPOS_GITHUB_REPO_OWNER") or None)
    )
    try:
        token = asyncio.run(_mint_token(resolved_owner))
    except _AmbiguousConnection:
        print(
            "github_auth: multiple GitHub connections and no repo owner to "
            "resolve by; pass --owner/--repo, or set "
            "SUPERPOS_GITHUB_CONNECTION_ID (or SUPERPOS_GITHUB_REPO_OWNER) to "
            "pin one.",
            file=sys.stderr,
        )
        return 1
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

    tok = sub.add_parser("token", help="Print a fresh GitHub token to stdout.")
    tok.add_argument(
        "--owner",
        help="Repo owner (org/user login) to mint the owning connection's "
        "token for, on multi-connection agents.",
    )
    tok.add_argument(
        "--repo",
        help="An owner/repo slug or a git remote URL; the owner is parsed out "
        "for owner-aware minting (e.g. --repo \"$(git config --get "
        "remote.origin.url)\").",
    )

    args = parser.parse_args(argv)
    if args.command == "setup":
        return cmd_setup()
    if args.command == "credential":
        return cmd_credential(args.action)
    if args.command == "token":
        return cmd_token(owner=args.owner, repo=args.repo)
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
