"""Registry sync — Phase 2 of the Registry subsystem (Superpos-AI/superpos-app #714).

The Registry promotes Subagents, Skills, and Modules to first-class hive
artifacts.  Phase 1 (superpos-app PR #715, now on main) shipped the
server-side data model, CRUD API, attachments, and
``GET /registry/resolved`` with scope-precedence resolution.

This module implements the **agent-side runtime sync** described in §8 of
``docs/proposals/registry.md``:

- :func:`sync_agent_scope` — startup sync against hive+agent scopes,
  diffs the resolved set against a shared install directory, and
  installs / uninstalls / reinstalls items so the on-disk state matches
  the resolver's truth.

- :func:`sync_task_scope` — task-claim sync that materialises only
  ``resolved_from_scope == "task"`` items into a per-task sandbox at
  ``/tmp/registry/<task_id>/<kind>/<slug>``.  Returns a teardown handle
  the task lifecycle calls on completion / failure so the sandbox is
  cleaned up without touching shared installs.

- :func:`resolve_path` — ordered overlay lookup helper.  The lookup
  order is ``task sandbox → shared root``; non-overridden items remain
  visible inside a task while task-scoped overrides win on slug
  collision.  Not yet wired into the existing subagent / skill loaders
  — this PR only ships the helper plus tests; the loader rewire lands
  in a follow-up.

Everything in this module is gated by the
``SUPERPOS_REGISTRY_SYNC_ENABLED`` env var (also accepts
``superpos.registry.sync_enabled`` via :class:`RegistrySyncConfig`).
The flag defaults to **off** so existing runtime behaviour
(``sub_agent_sync``, ``runtime-bundle``) is untouched until each agent
opts in.

Out of scope for this PR (tracked as follow-ups in the PR body):

- Replacing the ``runtime-bundle`` consumer in ``sub_agent_sync`` with
  the registry-backed source.
- Subscribing to the optional refresh-signal channel — sync currently
  only runs on startup + task claim.
- The actual rewire of the subagent / skill loaders to call
  :func:`resolve_path` at read time.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import httpx
import yaml

log = logging.getLogger(__name__)


# ── Public constants ─────────────────────────────────────────────────

#: Env var that gates the whole subsystem.  Off by default.
FEATURE_FLAG_ENV = "SUPERPOS_REGISTRY_SYNC_ENABLED"

#: Root for per-task ephemeral sandboxes.  See §8 of the proposal.
TASK_SANDBOX_ROOT = "/tmp/registry"

#: Marker dropped next to every shared-root install so :func:`sync_agent_scope`
#: can tell apart "this slug is here because the registry put it here" from
#: "this slug pre-existed (e.g. a workspace skill written by hand)".  Reading
#: a non-managed slug from the shared root is fine — the resolver simply
#: doesn't touch it.  We *only* remove files that carry this marker.
MANAGED_MARKER_FILENAME = ".registry-managed"

#: Supported kinds.  Mirrors ``RegistryItem.kind`` on the server.
#: ``module`` is included for the agent-scope path (modules can be
#: hive/agent-attached); the server rejects ``scope=task`` for modules
#: (v1 restriction, see proposal §8), so task-scope sync only ever sees
#: subagents / skills.
SUPPORTED_KINDS = ("subagent", "skill", "module")


# ── Config ───────────────────────────────────────────────────────────


def feature_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True iff the registry sync feature flag is set to a truthy value.

    Accepts ``1``, ``true``, ``yes``, ``on`` (case-insensitive).  Anything
    else — including unset — disables the feature, matching the
    proposal's "default off" requirement.
    """
    src = env if env is not None else os.environ
    value = src.get(FEATURE_FLAG_ENV, "").strip().lower()
    return value in ("1", "true", "yes", "on")


@dataclass
class RegistrySyncConfig:
    """Per-call configuration for :func:`sync_agent_scope` / :func:`sync_task_scope`.

    Mirrors the layout used by ``sub_agent_sync`` (caller passes URL +
    token + a shared root) but keeps the new subsystem self-contained
    so the eventual cutover from ``runtime-bundle`` to ``/registry/
    resolved`` can swap implementations without touching call sites.

    ``shared_roots`` maps each kind to the per-kind shared install
    directory.  The agent owns these paths (typically
    ``<workdir>/.<agent>/subagents``, ``<workdir>/.<agent>/skills``,
    ``<workdir>/.<agent>/modules``).
    """

    base_url: str
    token: str
    agent_id: str
    shared_roots: dict[str, str]
    sandbox_root: str = TASK_SANDBOX_ROOT
    http_timeout: float = 30.0


# ── HTTP client (sync, mirrors sub_agent_sync's style) ───────────────


class _ResolverClient(Protocol):
    """Minimal protocol so tests can inject a fake without httpx."""

    def fetch_resolved(
        self, agent_id: str, task_id: str | None = None,
    ) -> dict[str, Any]: ...


class RegistryResolverClient:
    """Sync httpx wrapper around ``GET /registry/resolved``.

    Returns the parsed response envelope from
    :class:`RegistryService::resolve` (the ``items`` list +
    ``agent_context``).  Raises :class:`RegistryFetchError` on any
    transport / decode / status failure — callers must treat that as
    "skip this sync cycle, leave existing state alone" (same defensive
    posture ``sub_agent_sync`` takes).
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

    def fetch_resolved(
        self, agent_id: str, task_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"agent_id": agent_id}
        if task_id:
            params["task_id"] = task_id
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                follow_redirects=True,
            ) as client:
                resp = client.get(
                    "/api/v1/registry/resolved",
                    params=params,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise RegistryFetchError(f"transport failure: {exc}") from exc

        if resp.status_code != 200:
            raise RegistryFetchError(
                f"/registry/resolved returned {resp.status_code}: "
                f"{resp.text[:200]}",
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise RegistryFetchError(f"response not JSON: {exc}") from exc

        # The server wraps every response in {"data": ...} via the
        # SuccessResponse trait.  Be permissive: accept both shapes so
        # tests don't have to mimic the envelope.
        payload = body.get("data", body) if isinstance(body, dict) else {}
        if not isinstance(payload, dict) or "items" not in payload:
            raise RegistryFetchError("response missing 'items' field")
        return payload


class RegistryFetchError(RuntimeError):
    """Raised when the resolver can't be reached or returns a bad shape.

    Distinguishes "real failure" from "empty desired set" so callers
    don't wipe managed files on a transient outage — same contract as
    :class:`sub_agent_sync.SubAgentFetchError`.
    """


# ── Lookup overlay ───────────────────────────────────────────────────


def resolve_path(
    kind: str,
    slug: str,
    *,
    shared_root: str,
    task_id: str | None = None,
    sandbox_root: str = TASK_SANDBOX_ROOT,
) -> Path | None:
    """Ordered overlay lookup for a single ``(kind, slug)`` entry.

    Lookup order matches §8 "Lookup contract" of the proposal:

    1. ``<sandbox_root>/<task_id>/<kind>/<slug>`` — task-scope override
       (only checked when ``task_id`` is supplied; this is what makes
       task-scoped attachments invisible to other concurrent tasks).
    2. ``<shared_root>/<slug>`` — agent + hive scope install written by
       :func:`sync_agent_scope`.

    Returns the first path that exists, or ``None`` if neither layer has
    it.  The returned path is always absolute.

    The overlay is read-only — it never creates files.  Both layers
    are produced by ``sync_*`` helpers in this module; the helper is
    intentionally separate so the subagent / skill loaders can be
    rewired piecemeal in follow-up PRs without circular dependencies.

    A slug is matched both as a bare filename (``<slug>``) and as
    ``<slug>.md`` — subagents land as ``<slug>.md`` files today and
    skills land as directories named ``<slug>``.  Picking the right
    suffix is the caller's concern in general, but accepting both here
    keeps the helper usable for either primitive without the caller
    needing two code paths.
    """
    if task_id:
        task_dir = Path(sandbox_root) / task_id / kind
        for candidate in (task_dir / slug, task_dir / f"{slug}.md"):
            if candidate.exists():
                return candidate.resolve()

    shared = Path(shared_root)
    for candidate in (shared / slug, shared / f"{slug}.md"):
        if candidate.exists():
            return candidate.resolve()
    return None


# ── Materialisation primitives ───────────────────────────────────────


@dataclass(frozen=True)
class ResolvedItem:
    """A single entry from ``/registry/resolved``, normalised."""

    kind: str
    slug: str
    name: str
    revision_id: str | None
    payload: dict[str, Any]
    resolved_from_scope: str
    resolved_from_attachment_id: str
    deleted_at: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "ResolvedItem":
        return cls(
            kind=str(raw["kind"]),
            slug=str(raw["slug"]),
            name=str(raw.get("name") or raw["slug"]),
            revision_id=raw.get("revision_id"),
            payload=raw.get("payload") or {},
            resolved_from_scope=str(raw.get("resolved_from_scope") or "hive"),
            resolved_from_attachment_id=str(
                raw.get("resolved_from_attachment_id") or "",
            ),
            deleted_at=raw.get("deleted_at"),
        )

    @property
    def revision_marker(self) -> str:
        """Stable identity string used to detect drift in the shared root.

        Combines ``revision_id`` (preferred — present whenever the
        attachment pinned a revision) and ``resolved_from_attachment_id``
        (always present) so unpinned "latest" attachments still trigger
        a reinstall when the underlying revision changes.  We can't
        rely on payload hashing alone because we want O(1) drift checks
        without re-rendering the file content.
        """
        return f"{self.revision_id or 'latest'}|{self.resolved_from_attachment_id}"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_managed_marker(install_dir: Path, item: ResolvedItem) -> None:
    """Write the ``.registry-managed`` sidecar that records how we got here.

    Contains the slug, revision marker, and resolved-from scope so a
    later sync can decide between "reinstall" (drift) and "uninstall"
    (no longer in the desired set) without re-fetching.  JSON because
    it's small and the install dir is filesystem-only.
    """
    marker = install_dir / MANAGED_MARKER_FILENAME
    marker.write_text(
        json.dumps(
            {
                "kind": item.kind,
                "slug": item.slug,
                "revision_marker": item.revision_marker,
                "resolved_from_scope": item.resolved_from_scope,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _read_managed_marker(install_dir: Path) -> dict[str, str] | None:
    marker = install_dir / MANAGED_MARKER_FILENAME
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _install_subagent(install_dir: Path, item: ResolvedItem) -> None:
    """Write a subagent payload into ``install_dir/<slug>.md``.

    Matches the on-disk shape ``sub_agent_sync`` already uses (YAML
    frontmatter + markdown body) so a future cutover to the registry
    source can keep the same reader.  ``install_dir`` is the per-item
    directory (``<shared>/<slug>/`` for the shared root, or
    ``<sandbox>/<task>/subagent/<slug>/`` for the task sandbox), which
    lets us co-locate the ``.registry-managed`` marker without
    polluting the parent directory.
    """
    payload = item.payload or {}
    frontmatter = payload.get("frontmatter") or {}
    body = payload.get("body") or ""

    fm_data: dict[str, Any] = {"name": item.slug}
    if frontmatter.get("description"):
        fm_data["description"] = frontmatter["description"]
    if frontmatter.get("model"):
        fm_data["model"] = frontmatter["model"]
    if frontmatter.get("tools"):
        fm_data["tools"] = list(frontmatter["tools"])

    fm_yaml = yaml.safe_dump(
        fm_data, default_flow_style=False, sort_keys=False, allow_unicode=True,
    ).rstrip()

    content = f"---\n{fm_yaml}\n---\n\n{body}\n"
    (install_dir / f"{item.slug}.md").write_text(content, encoding="utf-8")


def _install_skill(install_dir: Path, item: ResolvedItem) -> None:
    """Materialise a skill: ``SKILL.md`` + every helper file from ``payload.files``.

    Skill files are content-encoded in the payload (``{path, content,
    mode}``).  We mkdir intermediate dirs so payloads can ship
    ``scripts/foo.sh`` etc, and we honour the optional unix ``mode``
    field for executables.  Paths containing ``..`` or starting with
    ``/`` are rejected as a defensive measure against a malicious
    server / hive admin trying to escape the install dir.
    """
    payload = item.payload or {}
    instructions = payload.get("instructions") or ""
    (install_dir / "SKILL.md").write_text(instructions, encoding="utf-8")

    for entry in payload.get("files") or []:
        rel = entry.get("path") or ""
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            log.warning(
                "Skipping unsafe skill file path %r in item %s/%s",
                rel, item.kind, item.slug,
            )
            continue
        target = install_dir / rel
        _ensure_dir(target.parent)
        target.write_text(entry.get("content") or "", encoding="utf-8")
        mode = entry.get("mode")
        if isinstance(mode, int):
            target.chmod(mode & 0o777)
        elif mode == "+x":
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_module(install_dir: Path, item: ResolvedItem) -> None:
    """Install a module payload by delegating to the existing installer hook.

    Phase 2 does not replace the existing module installer in
    :mod:`superpos_agent_core.module_setup`; it *wraps* it.  For now we
    materialise the manifest and bundled SKILL.md to disk in the same
    layout :class:`module_loader.ModuleInfo` expects (``module.yaml``
    + optional ``SKILL.md``); the heavyweight side effects (pip
    install, PATH symlinks, system-prompt update) are still driven by
    ``module_setup.run_setup`` from the agent's entrypoint.  Wiring
    those side-effect steps into the diff loop is a follow-up PR.

    Modules attached at task scope are rejected server-side (v1
    restriction, proposal §8), so we never see one in
    :func:`sync_task_scope`.  This installer is therefore only invoked
    from the shared-root path.
    """
    payload = item.payload or {}
    manifest = payload.get("manifest") or {}

    yaml_data: dict[str, Any] = {
        "description": manifest.get("description") or item.name,
        "env": list(manifest.get("env_keys") or []),
    }
    (install_dir / "module.yaml").write_text(
        yaml.safe_dump(yaml_data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    skill_md = payload.get("skill")
    if skill_md:
        (install_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")


_INSTALLERS: dict[str, Callable[[Path, ResolvedItem], None]] = {
    "subagent": _install_subagent,
    "skill": _install_skill,
    "module": _install_module,
}


def _materialise(target_root: Path, item: ResolvedItem) -> Path:
    """Write a single :class:`ResolvedItem` into ``target_root/<slug>/``.

    Returns the per-item install dir.  Caller is responsible for the
    outer ``<kind>`` layer (so a shared root can be flat per kind while
    a task sandbox is layered as ``<task>/<kind>/<slug>``).
    """
    install_dir = target_root / item.slug
    if install_dir.exists():
        shutil.rmtree(install_dir)
    _ensure_dir(install_dir)

    installer = _INSTALLERS.get(item.kind)
    if installer is None:
        raise ValueError(f"unsupported registry kind: {item.kind!r}")
    installer(install_dir, item)
    _write_managed_marker(install_dir, item)
    return install_dir


# ── Phase 1: Agent-scope startup sync ─────────────────────────────────


@dataclass
class AgentScopeSyncResult:
    """Per-kind summary of what changed at startup.

    Test-friendly: assertions just inspect ``installed`` / ``uninstalled``
    / ``reinstalled`` sets instead of scraping log lines.
    """

    installed: list[str] = field(default_factory=list)
    uninstalled: list[str] = field(default_factory=list)
    reinstalled: list[str] = field(default_factory=list)
    skipped_tombstoned: list[str] = field(default_factory=list)
    skipped: bool = False  # set when the feature flag is off


def _scan_shared_root(shared_root: Path) -> dict[str, dict[str, str] | None]:
    """List every managed install under ``shared_root`` with its marker.

    Unmanaged entries (no ``.registry-managed`` marker) are included with
    a ``None`` marker so the diff loop knows they exist but leaves them
    alone — see :func:`sync_agent_scope` "uninstall" branch.
    """
    if not shared_root.is_dir():
        return {}
    state: dict[str, dict[str, str] | None] = {}
    for entry in sorted(shared_root.iterdir()):
        if not entry.is_dir():
            # Subagents historically land as ``<slug>.md`` files (no
            # per-item dir).  Treat the bare file as an unmanaged
            # legacy install and leave it alone.
            continue
        state[entry.name] = _read_managed_marker(entry)
    return state


def sync_agent_scope(
    config: RegistrySyncConfig,
    *,
    client: _ResolverClient | None = None,
) -> dict[str, AgentScopeSyncResult]:
    """Phase 1: startup sync of hive + agent attachments.

    Idempotent.  When the feature flag is off, returns one
    ``AgentScopeSyncResult(skipped=True)`` per kind without touching
    the filesystem — important because existing agents (running
    ``sub_agent_sync``) must see *zero* behaviour change until they
    opt in.

    On fetch failure (transport / 4xx / 5xx / bad shape), the existing
    shared-root state is left intact and the error is logged.  This
    mirrors ``sub_agent_sync``'s defensive "don't wipe local state on
    a transient outage" stance.
    """
    results: dict[str, AgentScopeSyncResult] = {
        kind: AgentScopeSyncResult() for kind in config.shared_roots
    }

    if not feature_enabled():
        log.debug("Registry sync feature flag off; skipping agent-scope sync")
        for r in results.values():
            r.skipped = True
        return results

    resolver = client or RegistryResolverClient(
        config.base_url, config.token, timeout=config.http_timeout,
    )

    try:
        response = resolver.fetch_resolved(config.agent_id, task_id=None)
    except RegistryFetchError as exc:
        log.warning(
            "Registry agent-scope fetch failed (%s); leaving shared root untouched.",
            exc,
        )
        return results

    desired_by_kind: dict[str, dict[str, ResolvedItem]] = {
        kind: {} for kind in config.shared_roots
    }
    for raw in response.get("items") or []:
        try:
            item = ResolvedItem.from_api(raw)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed registry item: %s", exc)
            continue

        # Phase 1 is "hive + agent attachments only" — see proposal §8.
        # Task-scoped items in the desired set must be ignored here; they
        # land in the per-task sandbox via :func:`sync_task_scope`.
        if item.resolved_from_scope == "task":
            continue

        if item.kind not in config.shared_roots:
            log.debug(
                "Skipping resolved item %s/%s: no shared root configured for kind %r",
                item.kind, item.slug, item.kind,
            )
            continue

        desired_by_kind[item.kind][item.slug] = item

    for kind, shared_root_str in config.shared_roots.items():
        shared_root = Path(shared_root_str)
        _ensure_dir(shared_root)
        result = results[kind]

        current = _scan_shared_root(shared_root)
        desired = desired_by_kind[kind]

        for slug, item in desired.items():
            if item.deleted_at:
                # Tombstoned items are still resolved when a live
                # attachment points at them (proposal §7 "Delete
                # semantics").  Surface a warning but install
                # anyway — the binding is intentional and dropping it
                # would silently demote pinned tasks to latest.
                log.warning(
                    "registry.resolved.tombstoned_binding kind=%s slug=%s scope=%s",
                    item.kind, item.slug, item.resolved_from_scope,
                )
                result.skipped_tombstoned.append(slug)

            marker = current.get(slug)
            if marker is None and slug not in current:
                _materialise(shared_root, item)
                result.installed.append(slug)
                log.info("registry install kind=%s slug=%s", kind, slug)
            elif marker is None:
                # Unmanaged legacy dir — leave alone, don't shadow it.
                log.debug(
                    "registry sync: leaving unmanaged %s/%s in place",
                    kind, slug,
                )
            elif marker.get("revision_marker") != item.revision_marker:
                _materialise(shared_root, item)
                result.reinstalled.append(slug)
                log.info(
                    "registry reinstall kind=%s slug=%s revision=%s",
                    kind, slug, item.revision_marker,
                )
            # else: revision marker matches — no-op (idempotent).

        for slug, marker in current.items():
            if marker is None:
                # Never delete unmanaged dirs.
                continue
            if slug in desired:
                continue
            install_dir = shared_root / slug
            shutil.rmtree(install_dir, ignore_errors=True)
            result.uninstalled.append(slug)
            log.info("registry uninstall kind=%s slug=%s", kind, slug)

    return results


# ── Phase 2: Task-scope claim sync ───────────────────────────────────


@dataclass
class TaskScopeSyncResult:
    """Outcome of :func:`sync_task_scope` for one task claim.

    ``teardown`` is the callable the task lifecycle must invoke on
    completion / failure to ``rm -rf`` the sandbox.  Always present
    (a no-op when the feature flag is off or no overrides were needed)
    so callers don't have to branch on ``None``.
    """

    task_id: str
    sandbox_dir: Path | None
    materialised: list[ResolvedItem]
    teardown: Callable[[], None]
    skipped: bool = False  # feature flag off, or fetch failure


def _make_teardown(sandbox: Path | None) -> Callable[[], None]:
    """Build the cleanup closure for a task sandbox.

    Idempotent — calling teardown twice is safe.  The closure captures
    the absolute sandbox path so concurrent tasks tearing down out of
    order can't accidentally take out a sibling.
    """
    if sandbox is None:
        def _noop() -> None:
            return None
        return _noop

    abs_sandbox = sandbox.resolve()

    def _teardown() -> None:
        if abs_sandbox.exists():
            shutil.rmtree(abs_sandbox, ignore_errors=True)
            log.info("registry task sandbox torn down: %s", abs_sandbox)

    return _teardown


def sync_task_scope(
    config: RegistrySyncConfig,
    task_id: str,
    *,
    client: _ResolverClient | None = None,
) -> TaskScopeSyncResult:
    """Phase 2: materialise task-scoped overrides for one claimed task.

    Walks ``/registry/resolved?agent_id=X&task_id=Y``, picks only the
    items the server tagged with ``resolved_from_scope == "task"``, and
    writes them under ``<sandbox_root>/<task_id>/<kind>/<slug>/``.
    Returns a :class:`TaskScopeSyncResult` whose ``teardown`` callable
    cleans up the sandbox; the caller (task lifecycle) is responsible
    for invoking it on completion or failure.

    If there are no task-scoped overrides for this task — the common
    case — nothing is written and ``sandbox_dir`` is ``None``.  This is
    the documented "use agent-scoped items as-is" branch of §8 Phase 2.

    Feature-flag-off and fetch-failure both yield a result whose
    ``teardown`` is a no-op, so callers can blindly call it without
    branching.
    """
    sandbox = Path(config.sandbox_root) / task_id

    if not feature_enabled():
        return TaskScopeSyncResult(
            task_id=task_id,
            sandbox_dir=None,
            materialised=[],
            teardown=_make_teardown(None),
            skipped=True,
        )

    resolver = client or RegistryResolverClient(
        config.base_url, config.token, timeout=config.http_timeout,
    )

    try:
        response = resolver.fetch_resolved(config.agent_id, task_id=task_id)
    except RegistryFetchError as exc:
        log.warning(
            "Registry task-scope fetch failed for task %s (%s); "
            "running task against agent-scope only.",
            task_id, exc,
        )
        return TaskScopeSyncResult(
            task_id=task_id,
            sandbox_dir=None,
            materialised=[],
            teardown=_make_teardown(None),
            skipped=True,
        )

    overrides: list[ResolvedItem] = []
    for raw in response.get("items") or []:
        try:
            item = ResolvedItem.from_api(raw)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed registry item for task %s: %s", task_id, exc)
            continue
        if item.resolved_from_scope != "task":
            continue
        if item.kind == "module":
            # Defence in depth — the server rejects module task-scope
            # attachments at write time (proposal §8 v1 restriction).
            # If one shows up anyway, refuse to materialise it: the
            # whole point of the v1 restriction is that we don't have
            # task-local pip/PATH/env isolation.
            log.warning(
                "Refusing to materialise task-scope module %s for task %s "
                "(v1 restriction)",
                item.slug, task_id,
            )
            continue
        overrides.append(item)

    if not overrides:
        return TaskScopeSyncResult(
            task_id=task_id,
            sandbox_dir=None,
            materialised=[],
            teardown=_make_teardown(None),
        )

    _ensure_dir(sandbox)
    for item in overrides:
        kind_dir = sandbox / item.kind
        _ensure_dir(kind_dir)
        _materialise(kind_dir, item)
        log.info(
            "registry task-scope materialise kind=%s slug=%s task=%s",
            item.kind, item.slug, task_id,
        )

    return TaskScopeSyncResult(
        task_id=task_id,
        sandbox_dir=sandbox,
        materialised=overrides,
        teardown=_make_teardown(sandbox),
    )


# ── Convenience for callers that own a list of items already ────────


def materialise_items(
    items: Iterable[ResolvedItem],
    *,
    target_root: str | Path,
    layout: str = "flat",
) -> list[Path]:
    """Write a precomputed list of items to disk.

    ``layout='flat'`` writes ``<target_root>/<slug>/`` (the shape
    Phase 1 uses for a shared per-kind root).  ``layout='by-kind'``
    writes ``<target_root>/<kind>/<slug>/`` (the shape Phase 2 uses
    for the task sandbox).  Exposed so tests and the eventual
    loader-rewire PR can drive materialisation without going through
    the HTTP resolver.
    """
    root = Path(target_root)
    _ensure_dir(root)
    installed: list[Path] = []
    for item in items:
        if layout == "flat":
            installed.append(_materialise(root, item))
        elif layout == "by-kind":
            kind_dir = root / item.kind
            _ensure_dir(kind_dir)
            installed.append(_materialise(kind_dir, item))
        else:
            raise ValueError(f"unknown layout: {layout!r}")
    return installed


__all__ = [
    "AgentScopeSyncResult",
    "FEATURE_FLAG_ENV",
    "MANAGED_MARKER_FILENAME",
    "RegistryFetchError",
    "RegistryResolverClient",
    "RegistrySyncConfig",
    "ResolvedItem",
    "SUPPORTED_KINDS",
    "TASK_SANDBOX_ROOT",
    "TaskScopeSyncResult",
    "feature_enabled",
    "materialise_items",
    "resolve_path",
    "sync_agent_scope",
    "sync_task_scope",
]
