"""Registry overlay — Beat 2b of the Registry Skills + Modules track.

The platform (superpos-app) now SERVES skills + modules at
``GET /registry/resolved`` (Beat 2a).  This module is the **agent-side
consumer**: at startup it fetches the resolved set and overlays the
registry-served artifacts on top of the package's baked-in modules /
skills.

Design goals (from the proposal):

- **Flag-gated, default OFF** — ``PLATFORM_REGISTRY_SERVE_SKILLS_MODULES``.
  When off the agent's behaviour is *exactly* today's baked-in path:
  zero registry calls, zero filesystem changes from this module.  This
  is the instant-rollback guarantee — flip the flag off and the next
  restart is back to baked-in.

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


#: Env var that gates the whole Beat 2b overlay.  OFF by default — this is
#: the instant-rollback guarantee.  Accepts ``1``/``true``/``yes``/``on``
#: (case-insensitive), matching the convention used elsewhere in the
#: package (e.g. ``SUPERPOS_KNOWLEDGE_INJECT``).
FEATURE_FLAG_ENV = "PLATFORM_REGISTRY_SERVE_SKILLS_MODULES"

#: Structured log record emitted when a module fails to install after the
#: bounded retry.  Asserted by tests; grep-able in production logs.
MODULE_INSTALL_FAILED_EVENT = "registry.module_install_failed"

#: Number of *extra* attempts after the first install attempt for a single
#: module (so total attempts = 1 + _MODULE_INSTALL_RETRIES).
_MODULE_INSTALL_RETRIES = 1

#: Bounded backoff (seconds) between a module's failed attempt and its retry.
_MODULE_INSTALL_BACKOFF_SECONDS = 0.5


def feature_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True iff ``PLATFORM_REGISTRY_SERVE_SKILLS_MODULES`` is truthy.

    Anything other than ``1``/``true``/``yes``/``on`` — including unset —
    disables the overlay, matching the proposal's "default OFF" rollback
    requirement.
    """
    src = env if env is not None else os.environ
    value = (src.get(FEATURE_FLAG_ENV, "") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


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


# ── Top-level overlay entry point ────────────────────────────────────


@dataclass
class RegistryOverlayResult:
    """Outcome of :func:`apply_registry_overlay`.

    ``skipped`` is True when the feature flag is off — the instant-rollback
    state.  ``fetch_failed`` is True when the flag was on but the resolved
    payload was missing (fetch/parse failure) and we fell back to baked-in.
    """

    skipped: bool = False
    fetch_failed: bool = False
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
        # Fetch / HTTP / parse failure — degrade to baked-in.  The agent
        # still starts; baked-in modules + skills (already installed by the
        # caller's run_setup) remain the source of truth.
        log.warning(
            "Registry resolved fetch failed; falling back entirely to "
            "baked-in skills + modules (agent still starts)."
        )
        return RegistryOverlayResult(fetch_failed=True)

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

    # Re-run the existing install side effects so registry modules become
    # callable + documented — reusing the baked-in helpers, not a parallel
    # path.  Only modules that installed cleanly are on disk; skipped ones
    # were never materialised, so they're naturally excluded.
    if modules_result.installed:
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
    "ModuleOverlayResult",
    "RegistryOverlayResult",
    "SkillOverlayResult",
    "apply_registry_overlay",
    "feature_enabled",
    "overlay_modules",
    "overlay_skills",
]
