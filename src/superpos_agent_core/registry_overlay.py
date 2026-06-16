"""Registry overlay — Beat 2b of the Registry Skills + Modules track.

The platform (superpos-app) now SERVES skills + modules at
``GET /registry/resolved`` (Beat 2a).  This module is the **agent-side
consumer**: at startup it fetches the resolved set and overlays the
registry-served artifacts on top of the package's baked-in modules /
skills.

Design goals (from the proposal):

- **Flag-gated, default ON** — ``PLATFORM_REGISTRY_SERVE_SKILLS_MODULES``.
  The overlay is on by default; agents fetch and overlay the
  registry-served set without needing an explicit env override.  Setting
  the flag to an explicit falsey value (``0``/``false``/``no``/``off``)
  restores *exactly* today's baked-in path: zero registry calls, zero
  filesystem changes from this module.  This is the instant-rollback
  guarantee — set the flag explicitly false and the next restart is back
  to baked-in.

- **Overlay precedence** — a registry item wins over a baked-in one of
  the same slug (replace), while baked-in items absent from the registry
  remain.  Modules are materialised into the workspace modules dir (the
  same root :func:`module_setup.run_setup` overlays on top of bundled),
  so the existing bundled→workspace merge already gives "registry wins".

- **Cohesion** — we reuse the existing helpers
  (:func:`module_setup.symlink_module_scripts`,
  :func:`module_loader.discover_modules` / ``generate_modules_doc`` /
  :func:`module_setup.update_agents_md`) rather than building a parallel
  installer.

- **Secrets** — modules declare ``env_keys`` (NAMES only).  We never
  fetch, inject, or invent credential values; module scripts call the
  credential proxy at runtime exactly as today.  We only install the
  scripts / manifest / docs.

Failure handling (implemented exactly as the proposal specifies):

- **Fetch failure** (``resolved`` is ``None`` — network / HTTP / parse):
  log a warning and fall back entirely to baked-in.  The agent still
  starts; this module is a no-op overlay on top of the baked-in
  ``run_setup`` the caller already ran.

- **Per-module install failure**: retry once with a bounded backoff,
  then skip that module (no scripts symlinked, no doc injected), emit a
  structured ``registry.module_install_failed`` log record (with module
  slug + error), and continue with the remaining modules.  One flaky
  module must never brick the agent.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .module_loader import discover_modules, generate_modules_doc
from .module_setup import symlink_module_scripts, update_agents_md

log = logging.getLogger(__name__)


#: Structured log record emitted when the flag is ON, the live fetch failed,
#: there is no last-good cache, AND nothing baked-in is available — the
#: "degraded-empty" boot.  The agent still comes up and keeps polling so it
#: can self-heal once the registry recovers; it just has zero registry-served
#: skills/modules for now.  Asserted by tests; grep-able in production logs.
#: This is the safety net the Beat 4 lean (no baked-in) image relies on.
RESOLVED_EMPTY_NO_FALLBACK_EVENT = "registry.resolved_empty_no_fallback"


#: Env var that gates the whole Beat 2b overlay.  ON by default — agents
#: overlay the registry-served set without needing an env override.  An
#: explicit falsey value (``0``/``false``/``no``/``off``, case-insensitive)
#: disables the overlay; this is the instant-rollback guarantee.  Truthy
#: values (``1``/``true``/``yes``/``on``) and unset both enable it.
FEATURE_FLAG_ENV = "PLATFORM_REGISTRY_SERVE_SKILLS_MODULES"

#: Structured log record emitted when a module fails to install after the
#: bounded retry.  Asserted by tests; grep-able in production logs.
MODULE_INSTALL_FAILED_EVENT = "registry.module_install_failed"

#: Sentinel file dropped into every registry-materialised module dir so a
#: later restart can tell registry-managed installs apart from bundled /
#: hand-authored workspace modules.  This is what makes the instant-rollback
#: guarantee real: registry modules are written into the *persistent*
#: workspace ``modules_dir``, so a flag-OFF restart must be able to find and
#: remove exactly the registry-managed ones (and nothing else) before the
#: baked-in discover/symlink/doc path runs.  Bundled and hand-authored
#: modules never carry this marker, so they are never touched.
REGISTRY_MANAGED_MARKER = ".registry-overlay"

#: Structured log record emitted when a registry-managed module is removed
#: on a flag-OFF rollback sweep or a flag-ON reconcile (disappeared from the
#: resolved set).  Asserted by tests; grep-able in production logs.
MODULE_REMOVED_EVENT = "registry.module_removed"

#: Number of *extra* attempts after the first install attempt for a single
#: module (so total attempts = 1 + _MODULE_INSTALL_RETRIES).
_MODULE_INSTALL_RETRIES = 1

#: Bounded backoff (seconds) between a module's failed attempt and its retry.
_MODULE_INSTALL_BACKOFF_SECONDS = 0.5


def feature_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True unless ``PLATFORM_REGISTRY_SERVE_SKILLS_MODULES`` is falsey.

    Default-ON: an unset / empty var enables the overlay.  Only an explicit
    falsey value (``0``/``false``/``no``/``off``, case-insensitive and
    whitespace-trimmed) disables it — this is the rollback path.  Truthy
    values (``1``/``true``/``yes``/``on``) also enable it.
    """
    src = env if env is not None else os.environ
    value = (src.get(FEATURE_FLAG_ENV, "") or "").strip().lower()
    return value not in ("0", "false", "no", "off")


def _is_safe_slug(slug: str) -> bool:
    """Reject slugs that would escape their install root.

    A compromised server / malicious hive admin could ship ``slug`` like
    ``../../escape``; without this guard that would direct a write (or a
    later ``rmtree``) outside the agent-owned root.
    """
    if not isinstance(slug, str) or not slug or slug in (".", ".."):
        return False
    if "/" in slug or "\\" in slug or "\x00" in slug:
        return False
    return Path(slug).name == slug


def _safe_rel(rel: str) -> bool:
    """Return True if ``rel`` is a safe relative file path inside the dir."""
    return bool(rel) and not rel.startswith("/") and ".." not in Path(rel).parts


def _write_file_entry(install_dir: Path, entry: dict) -> None:
    """Write one ``{path, content, mode}`` file under ``install_dir``.

    Honours the optional unix ``mode`` (int octal, or the string ``"+x"``)
    so executable scripts land callable.  Unsafe paths are skipped with a
    warning rather than aborting the whole item.
    """
    rel = entry.get("path") or ""
    if not _safe_rel(rel):
        log.warning("Skipping unsafe registry file path %r", rel)
        return
    target = install_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(entry.get("content") or "", encoding="utf-8")
    mode = entry.get("mode")
    if isinstance(mode, bool):
        # bool is an int subclass — guard so a stray True isn't chmod 1.
        return
    if isinstance(mode, int):
        target.chmod(mode & 0o777)
    elif mode == "+x":
        target.chmod(
            target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )


# ── Skills overlay ───────────────────────────────────────────────────


@dataclass
class SkillOverlayResult:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def overlay_skills(skills: list[dict], skills_dir: str) -> SkillOverlayResult:
    """Write each registry skill to ``<skills_dir>/<slug>.md`` (+ its ``files[]``).

    Registry skill with the same slug as a baked-in one wins (the file is
    overwritten).  Baked-in skills absent from the registry remain
    untouched.  Per-skill failures are logged and skipped — a bad skill
    must not abort the rest.
    """
    result = SkillOverlayResult()
    root = Path(skills_dir)
    root.mkdir(parents=True, exist_ok=True)

    for skill in skills or []:
        slug = skill.get("slug")
        if not slug or not _is_safe_slug(slug):
            log.warning("registry skill: refusing unsafe/empty slug %r", slug)
            result.skipped.append(str(slug))
            continue
        try:
            instructions = skill.get("instructions") or ""
            (root / f"{slug}.md").write_text(instructions, encoding="utf-8")

            files = skill.get("files") or []
            if files:
                # Helper files (e.g. scripts/) live in a per-slug dir so they
                # don't collide with sibling skills.
                files_dir = root / slug
                files_dir.mkdir(parents=True, exist_ok=True)
                for entry in files:
                    _write_file_entry(files_dir, entry)
            result.written.append(slug)
            log.info("registry skill overlay: wrote %s", slug)
        except Exception as exc:  # noqa: BLE001 — isolate one bad skill
            log.warning("registry skill %s overlay failed: %s", slug, exc)
            result.skipped.append(slug)

    return result


# ── Modules overlay ──────────────────────────────────────────────────


@dataclass
class ModuleOverlayResult:
    installed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _materialise_module(module: dict, modules_dir: Path) -> Path:
    """Write one registry module into ``<modules_dir>/<slug>/``.

    Produces the on-disk layout :class:`module_loader.ModuleInfo` expects:

    - ``module.yaml`` — from ``manifest`` (description, ``env`` from
      ``env_keys`` NAMES only, optional ``mcp``).  **No credential values
      are ever read or written** — ``env_keys`` are names the runtime
      proxy resolves later.
    - ``scripts/<file>`` — from ``files[]`` (mode honoured so scripts are
      executable).
    - ``SKILL.md`` — from the module's ``skill`` field, if present.

    The new version is built in a sibling staging directory and only
    swapped into ``<modules_dir>/<slug>/`` once every write has succeeded.
    The swap preserves the previously-working install until the new one is
    actually active: any prior install is first moved aside to a sibling
    backup dir (``os.replace`` — atomic, same parent), then the staging dir
    is renamed into place.  On success the backup is removed; if the final
    rename fails the backup is restored to ``install_dir`` and the staging
    dir discarded, so a failed swap never loses a working module nor leaves
    stray staging/backup dirs behind.  If *any* write raises, the staging
    dir is removed and the error re-raised, leaving the existing install
    **untouched** — so the caller's retry/skip fallback never loses a
    previously-working module to a transient write error or a malformed
    registry payload.  Raises on any IO error so the caller's retry/skip
    loop can react.
    """
    slug = module["slug"]
    if not _is_safe_slug(slug):
        raise ValueError(f"unsafe module slug {slug!r}")

    install_dir = modules_dir / slug
    # Build into a sibling staging dir so a mid-write failure can't damage
    # an existing install; the same parent makes the final rename atomic.
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{slug}.tmp-", dir=modules_dir)
    )
    try:
        manifest = module.get("manifest") or {}
        yaml_data: dict = {
            "description": manifest.get("description") or module.get("name") or slug,
            # env_keys are NAMES ONLY — never values.  module_loader reads this
            # into ModuleInfo.env_vars; the runtime proxy supplies values.
            "env": list(manifest.get("env_keys") or []),
        }
        mcp_cfg = manifest.get("mcp")
        if mcp_cfg is not None:
            yaml_data["mcp"] = mcp_cfg
        (staging_dir / "module.yaml").write_text(
            yaml.safe_dump(yaml_data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

        for entry in module.get("files") or []:
            _write_file_entry(staging_dir, entry)

        skill_md = module.get("skill")
        if skill_md:
            (staging_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

        # Provenance marker — written *inside* the atomic staging build so it
        # is swapped into place together with the module's content.  A later
        # flag-OFF restart removes exactly the dirs carrying this sentinel.
        (staging_dir / REGISTRY_MANAGED_MARKER).write_text("", encoding="utf-8")
    except BaseException:
        # New version is incomplete — discard the staging dir and leave the
        # existing install (if any) exactly as it was.
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    # New version fully materialised — swap it into place while keeping the
    # previously-working install recoverable until the new one is active.
    if install_dir.exists():
        # Move the old install aside first so a failed final rename never
        # leaves us with *no* install.  Same parent dir keeps both renames
        # atomic.
        backup_dir = Path(
            tempfile.mkdtemp(prefix=f".{slug}.bak-", dir=modules_dir)
        )
        # mkdtemp created an empty dir; os.replace needs the target absent.
        os.rmdir(backup_dir)
        os.replace(install_dir, backup_dir)
        try:
            os.replace(staging_dir, install_dir)
        except BaseException:
            # Final swap failed — restore the previously-working install and
            # discard the staging dir so nothing is left behind.
            os.replace(backup_dir, install_dir)
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        # New version is live — drop the backup of the old install.
        shutil.rmtree(backup_dir, ignore_errors=True)
    else:
        # First install — no prior version to preserve.
        try:
            os.replace(staging_dir, install_dir)
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    return install_dir


def overlay_modules(
    modules: list[dict],
    modules_dir: str,
    *,
    backoff_seconds: float = _MODULE_INSTALL_BACKOFF_SECONDS,
    sleep=time.sleep,
) -> ModuleOverlayResult:
    """Materialise each registry module into the workspace ``modules_dir``.

    Materialising into the workspace modules root means the existing
    bundled→workspace merge in :func:`module_loader.discover_modules`
    already gives the required overlay precedence: a registry module with
    the same slug as a bundled (baked-in) one shadows it, while bundled
    modules absent from the registry remain.

    Per-module failure handling (proposal §4): retry once with a bounded
    backoff, then skip the module and emit a structured
    ``registry.module_install_failed`` record.  A skipped module is left
    out of ``installed`` so its scripts are never symlinked and its doc is
    never injected — but the loop continues with the other modules.
    """
    result = ModuleOverlayResult()
    root = Path(modules_dir)
    root.mkdir(parents=True, exist_ok=True)

    for module in modules or []:
        slug = module.get("slug") or "<unknown>"
        last_exc: Exception | None = None
        for attempt in range(_MODULE_INSTALL_RETRIES + 1):
            try:
                _materialise_module(module, root)
                result.installed.append(slug)
                log.info("registry module overlay: installed %s", slug)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 — isolate one bad module
                last_exc = exc
                if attempt < _MODULE_INSTALL_RETRIES:
                    log.warning(
                        "registry module %s install attempt %d failed: %s; retrying",
                        slug, attempt + 1, exc,
                    )
                    if backoff_seconds > 0:
                        sleep(backoff_seconds)
        if last_exc is not None:
            # Bounded retries exhausted — skip and continue.
            result.failed.append(slug)
            log.warning(
                "%s slug=%s error=%s",
                MODULE_INSTALL_FAILED_EVENT, slug, last_exc,
                extra={
                    "event": MODULE_INSTALL_FAILED_EVENT,
                    "module_slug": slug,
                    "error": str(last_exc),
                },
            )

    return result


def _unlink_module_bin_symlinks(module_dir: Path, bin_dir: Path) -> None:
    """Unlink any ``bin_dir`` symlink that points into ``module_dir``.

    Registry module scripts are symlinked onto a PATH dir by
    :func:`module_setup.symlink_module_scripts` (target = the absolute
    ``<module_dir>/scripts/<name>``).  When the module dir is removed those
    links dangle, so the rollback must clear them too — otherwise a removed
    registry module's command name stays resolvable on PATH (pointing at a
    now-missing target).  We match by the symlink's *target* (via
    ``realpath``, which works even once the link is broken) so we never
    remove an unrelated link that merely shares a basename.
    """
    if not bin_dir.is_dir():
        return
    module_prefix = str(module_dir) + os.sep
    for link in sorted(bin_dir.iterdir()):
        if not link.is_symlink():
            continue
        target = os.path.realpath(link)
        if target == str(module_dir) or target.startswith(module_prefix):
            try:
                link.unlink()
            except OSError as exc:  # pragma: no cover — defensive
                log.warning("Could not unlink stale bin symlink %s: %s", link, exc)


def remove_registry_overlay_modules(
    modules_dir: str,
    *,
    bin_dir: str | None = None,
    keep: set[str] | None = None,
) -> list[str]:
    """Remove registry-materialised modules from the workspace ``modules_dir``.

    Only directories carrying the :data:`REGISTRY_MANAGED_MARKER` sentinel
    are removed — bundled and hand-authored workspace modules never carry
    it, so they are left untouched.  Any ``bin_dir`` symlink pointing into a
    removed module is unlinked too, so the removal also clears the module's
    scripts from PATH.

    ``keep`` — when provided, registry-managed modules whose slug is in this
    set are retained and everything else managed is removed (the flag-ON
    *reconcile* against the current resolved set).  When ``None`` (default)
    **all** registry-managed modules are removed (the flag-OFF instant
    rollback sweep).

    Returns the sorted list of removed slugs.
    """
    root = Path(modules_dir)
    if not root.is_dir():
        return []
    bin_path = Path(bin_dir) if bin_dir else None
    removed: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        # Guard against a crafted dir name escaping the root before any
        # destructive rmtree.
        if not _is_safe_slug(entry.name):
            continue
        if not (entry / REGISTRY_MANAGED_MARKER).is_file():
            continue
        if keep is not None and entry.name in keep:
            continue
        module_abs = entry.resolve()
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            log.warning("Failed to remove registry module %s: %s", entry.name, exc)
            continue
        if bin_path is not None:
            _unlink_module_bin_symlinks(module_abs, bin_path)
        removed.append(entry.name)
        log.info(
            "%s slug=%s reason=%s",
            MODULE_REMOVED_EVENT,
            entry.name,
            "flag_off_rollback" if keep is None else "reconcile_absent",
            extra={
                "event": MODULE_REMOVED_EVENT,
                "module_slug": entry.name,
                "reason": "flag_off_rollback" if keep is None else "reconcile_absent",
            },
        )
    return removed


# ── Top-level overlay entry point ────────────────────────────────────


def _has_baked_in_fallback(modules_dir: str, skills_dir: str | None) -> bool:
    """True when *something* is already on disk to fall back to.

    Used only on a failed fetch with no cache to decide between a benign
    "degrade to baked-in" and the "degraded-empty" boot.  We count any
    discoverable module (bundled or workspace) and any ``<slug>.md`` skill
    already written under ``skills_dir``.  Beat 4 drops the baked-in
    artifacts, so on the lean image this returns False and the degraded-empty
    path lights up — exactly the case this PR makes safe.
    """
    try:
        if discover_modules(modules_dir if os.path.isdir(modules_dir) else None):
            return True
    except Exception:  # noqa: BLE001 — discovery must never break the boot decision
        pass
    if skills_dir:
        skills_root = Path(skills_dir)
        if skills_root.is_dir() and any(skills_root.glob("*.md")):
            return True
    return False


@dataclass
class RegistryOverlayResult:
    """Outcome of :func:`apply_registry_overlay`.

    ``skipped`` is True when the feature flag is off — the instant-rollback
    state.  ``fetch_failed`` is True when the flag was on but the resolved
    payload was missing (fetch/parse failure) and we fell back to baked-in.
    ``degraded_empty`` is True in the worst case: fetch failed, no last-good
    cache, AND nothing baked-in available — the agent boots with zero
    registry skills/modules and keeps polling so it self-heals once the
    registry recovers.  It implies ``fetch_failed``.
    """

    skipped: bool = False
    fetch_failed: bool = False
    degraded_empty: bool = False
    skills: SkillOverlayResult = field(default_factory=SkillOverlayResult)
    modules: ModuleOverlayResult = field(default_factory=ModuleOverlayResult)


def apply_registry_overlay(
    resolved: dict | None,
    *,
    modules_dir: str,
    skills_dir: str | None = None,
    agents_md_path: str | None = None,
    bin_dir: str | None = None,
    env: dict[str, str] | None = None,
) -> RegistryOverlayResult:
    """Overlay registry-served skills + modules on top of the baked-in set.

    This is the Beat 2b entry point.  It assumes the caller has *already*
    run the baked-in :func:`module_setup.run_setup` (so bundled modules +
    their docs/symlinks are in place); this function then overlays the
    registry items on top.

    Parameters:
        resolved: the parsed ``/registry/resolved`` payload (grouped
            ``skills`` / ``modules`` keys), or ``None`` if the fetch
            failed.  ``None`` → degraded fall-back to baked-in.
        modules_dir: workspace modules root (registry modules land here,
            shadowing bundled ones of the same slug).  Modules are
            **always** overlaid when the flag is on — they target this
            workspace dir and don't depend on ``skills_dir``.
        skills_dir: workspace skills root (``<slug>.md`` files written
            here).  Optional — when ``None`` the **skills** portion of the
            overlay is skipped (a structured warning is logged) but modules
            are still overlaid.  This decouples module rollout from a
            startup command that doesn't pass a skills dir.
        agents_md_path: optional system-prompt file to re-render the
            module doc block into after installing registry modules.
        bin_dir: optional PATH dir to (re-)symlink module scripts into.
        env: optional env mapping (for the flag); defaults to ``os.environ``.

    When the flag is OFF this returns immediately with ``skipped=True`` and
    makes **zero** filesystem changes and **zero** registry use — the
    instant-rollback guarantee.
    """
    if not feature_enabled(env):
        log.debug(
            "%s off; skipping registry overlay (baked-in only)", FEATURE_FLAG_ENV
        )
        return RegistryOverlayResult(skipped=True)

    if resolved is None:
        # Live fetch failed AND no last-good cache (the fetch layer already
        # tried the cache before handing us ``None``).  We never raise here:
        # a polling agent MUST come up even with zero skills/modules so it can
        # self-heal once the registry recovers.  The destructive reconcile is
        # *not* run on this path, so a transient outage can't wipe a
        # previously-installed registry module.
        if _has_baked_in_fallback(modules_dir, skills_dir):
            # Benign degrade — baked-in (bundled / already-installed) artifacts
            # remain the source of truth, exactly as today.
            log.warning(
                "Registry resolved fetch failed; falling back entirely to "
                "baked-in skills + modules (agent still starts)."
            )
            return RegistryOverlayResult(fetch_failed=True)
        # Degraded-empty: nothing live, nothing cached, nothing baked-in.
        # Boot anyway with zero registry artifacts and keep polling.
        log.warning(
            "%s — registry fetch failed, no last-good cache, and no baked-in "
            "skills/modules available; booting DEGRADED with zero registry "
            "artifacts and continuing to poll (will self-heal when the "
            "registry recovers).",
            RESOLVED_EMPTY_NO_FALLBACK_EVENT,
            extra={
                "event": RESOLVED_EMPTY_NO_FALLBACK_EVENT,
                "modules_dir": modules_dir,
                "skills_dir": skills_dir,
            },
        )
        return RegistryOverlayResult(fetch_failed=True, degraded_empty=True)

    skill_items = resolved.get("skills") or []
    module_items = resolved.get("modules") or []

    # Skills need a target dir; modules don't (they land in modules_dir).
    # Decoupling the two means a flag-on startup command without a skills
    # dir still gets its registry modules — only the skills half is skipped.
    if skills_dir is not None:
        skills_result = overlay_skills(skill_items, skills_dir)
    else:
        skills_result = SkillOverlayResult(skipped=[s.get("slug") for s in skill_items])
        if skill_items:
            log.warning(
                "registry.skills_overlay_skipped reason=no_skills_dir count=%d; "
                "modules still overlaid",
                len(skill_items),
                extra={
                    "event": "registry.skills_overlay_skipped",
                    "reason": "no_skills_dir",
                    "skill_count": len(skill_items),
                },
            )
    modules_result = overlay_modules(module_items, modules_dir)

    # Reconcile: drop registry-managed modules that are no longer in the
    # resolved set (removed / unauthorised upstream) so they don't linger on
    # disk and PATH.  Only runs because we have a fresh, authoritative
    # ``resolved`` here — a fetch failure short-circuits above, so a transient
    # outage never wipes a previously-working registry module.  ``keep`` is
    # every slug the registry still serves, including ones that failed to
    # (re)install this round, so a flaky install never deletes a good module.
    keep = {
        m.get("slug")
        for m in module_items
        if isinstance(m, dict) and _is_safe_slug(m.get("slug"))
    }
    removed = remove_registry_overlay_modules(
        modules_dir, bin_dir=bin_dir, keep=keep
    )

    # Re-run the existing install side effects so registry modules become
    # callable + documented — reusing the baked-in helpers, not a parallel
    # path.  Only modules that installed cleanly are on disk; skipped ones
    # were never materialised, so they're naturally excluded.  A reconcile
    # removal also requires a re-render so the dropped module leaves the docs.
    if modules_result.installed or removed:
        if bin_dir:
            symlink_module_scripts(modules_dir, bin_dir)
        if agents_md_path:
            merged = discover_modules(
                modules_dir if os.path.isdir(modules_dir) else None
            )
            update_agents_md(generate_modules_doc(merged), agents_md_path)

    return RegistryOverlayResult(
        skipped=False,
        fetch_failed=False,
        skills=skills_result,
        modules=modules_result,
    )


__all__ = [
    "FEATURE_FLAG_ENV",
    "MODULE_INSTALL_FAILED_EVENT",
    "MODULE_REMOVED_EVENT",
    "REGISTRY_MANAGED_MARKER",
    "RESOLVED_EMPTY_NO_FALLBACK_EVENT",
    "ModuleOverlayResult",
    "RegistryOverlayResult",
    "SkillOverlayResult",
    "apply_registry_overlay",
    "feature_enabled",
    "overlay_modules",
    "overlay_skills",
    "remove_registry_overlay_modules",
]
