"""Persona + memory overlay — AG-10 (issue #193).

Extends the Beat 2b *doubling* pattern (:mod:`registry_overlay`) from the
modules / skills path to the **persona** and **memory** path.  The shape is
deliberately identical: Superpos is the primary source, the agent image carries
a baked-in snapshot as the fallback, and the two never diverge silently.

Today the persona is fetched once at startup
(:meth:`superpos_client.SuperposClient.get_persona_assembled`) and, if Superpos
is unreachable, the agent boots with **no persona at all**.  Memory is
write-only (:meth:`update_persona_memory`) with no read fallback.  This module
closes that gap.

Design goals (mirroring :mod:`registry_overlay`):

- **Flag-gated, default ON** — :data:`FEATURE_FLAG_ENV`
  (``PLATFORM_PERSONA_MEMORY_DOUBLING``).  An explicit falsey value
  (``0``/``false``/``no``/``off``) restores *exactly* today's behaviour: the
  fetched persona passes straight through, zero snapshot reads or writes.  This
  is the instant-rollback guarantee.

- **Two-layer snapshot** — a **bundled** snapshot baked into the package
  (``snapshots/persona.md`` + ``snapshots/MEMORY.md``, the floor a never-online
  agent still gets) and a **workspace** snapshot
  (``<snapshot_dir>/persona.md`` …) that is *re-synced* on every reachable
  startup so the fallback is always the last-known-good, not a stale build
  artifact.  Read precedence: **Superpos → workspace snapshot → bundled
  snapshot**, exactly mirroring the bundled→workspace module overlay.

- **Read-side degradation only** — persona + memory *reads* fall back to the
  snapshot; memory *writes* stay Superpos-only and fail **loudly** on an outage.
  There is no agent-local silent fallback (that would double-write a rule and
  diverge the two layers).  This matches the modules pattern, where rollback is
  read-side only.

Failure handling (same contract as modules):

- **Persona fetch failure** (``fetched_persona`` is ``None``): log a warning,
  serve the snapshot (workspace, else bundled), the agent still starts.

- **Memory read, Superpos down**: serve the snapshot (read-only default rules).

- **Memory write, Superpos down**: raise :class:`MemoryWriteUnavailable` — never
  write a local fallback.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


#: Env var that gates the whole persona/memory doubling overlay.  ON by default
#: — an unset / empty / truthy value enables it; only an explicit falsey value
#: (``0``/``false``/``no``/``off``, case-insensitive) disables it and restores
#: today's exact behaviour.  Mirrors
#: :data:`registry_overlay.FEATURE_FLAG_ENV`.
FEATURE_FLAG_ENV = "PLATFORM_PERSONA_MEMORY_DOUBLING"

#: Filenames inside both the bundled (``snapshots/``) and workspace snapshot
#: dirs.  Kept identical across the two layers so resolution is a simple
#: precedence walk.
PERSONA_SNAPSHOT_FILENAME = "persona.md"
MEMORY_SNAPSHOT_FILENAME = "MEMORY.md"

#: Sidecar holding the unix timestamp of the last successful Superpos memory
#: fetch, so :func:`read_memory` can honour a TTL without re-hitting the API on
#: every read.
MEMORY_CACHE_META_FILENAME = ".memory-cache.json"

#: Default TTL (seconds) for the memory read cache.  Short enough to re-sync
#: promptly after a persona edit / recovery, long enough to avoid hammering the
#: API on repeated reads.
DEFAULT_MEMORY_TTL_SECONDS = 300

#: Structured log records — asserted by tests, grep-able in production logs.
PERSONA_RESYNCED_EVENT = "persona.snapshot_resynced"
PERSONA_FETCH_FAILED_EVENT = "persona.fetch_failed"
MEMORY_FETCH_FAILED_EVENT = "persona.memory_fetch_failed"
MEMORY_WRITE_NO_FALLBACK_EVENT = "persona.memory_write_no_fallback"


def feature_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True unless ``PLATFORM_PERSONA_MEMORY_DOUBLING`` is falsey.

    Default-ON, identical semantics to
    :func:`registry_overlay.feature_enabled`: an unset / empty var enables the
    overlay; only an explicit falsey value (``0``/``false``/``no``/``off``,
    case-insensitive and whitespace-trimmed) disables it.
    """
    src = env if env is not None else os.environ
    value = (src.get(FEATURE_FLAG_ENV, "") or "").strip().lower()
    return value not in ("0", "false", "no", "off")


def bundled_snapshot_dir() -> Path:
    """Directory of the package-baked snapshot floor (``snapshots/``)."""
    return Path(__file__).resolve().parent / "snapshots"


def _read_text(path: Path) -> str | None:
    """Return the file's text, or ``None`` if it is absent / unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _write_text_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (tmp in same dir + ``os.replace``).

    Same-parent temp keeps the rename atomic, so a crashed / concurrent restart
    never observes a half-written snapshot.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Persona ──────────────────────────────────────────────────────────


@dataclass
class PersonaOverlayResult:
    """Outcome of :func:`apply_persona_overlay`.

    ``skipped`` — flag off; ``fetched_persona`` passed through untouched.
    ``fetch_failed`` — flag on but Superpos returned nothing; the snapshot was
    served.  ``source`` is one of ``superpos`` / ``snapshot_workspace`` /
    ``snapshot_bundled`` / ``none``.  ``persona`` is the persona the caller
    should actually use (may be ``None`` only when no snapshot exists either).
    """

    skipped: bool = False
    fetch_failed: bool = False
    source: str = "none"
    persona: str | None = None


def apply_persona_overlay(
    fetched_persona: str | None,
    *,
    snapshot_dir: str,
    bundled_dir: str | None = None,
    env: dict[str, str] | None = None,
) -> PersonaOverlayResult:
    """Resolve the effective persona, re-syncing / falling back to a snapshot.

    Call this right after :meth:`SuperposClient.get_persona_assembled` at
    startup, passing its result (``None`` on an outage).

    - **Flag OFF** → return immediately with ``skipped=True`` and
      ``persona=fetched_persona``: today's exact behaviour, zero snapshot IO.
    - **Persona fetched** → persist it to the workspace snapshot (the re-sync)
      and return it (``source="superpos"``).
    - **Fetch failed** (``None``) → serve the workspace snapshot, else the
      bundled snapshot; ``fetch_failed=True``, a warning is logged, the agent
      still starts.
    """
    if not feature_enabled(env):
        return PersonaOverlayResult(
            skipped=True,
            source="superpos" if fetched_persona is not None else "none",
            persona=fetched_persona,
        )

    ws = Path(snapshot_dir)
    persona_ws = ws / PERSONA_SNAPSHOT_FILENAME
    bundled = Path(bundled_dir) if bundled_dir else bundled_snapshot_dir()
    persona_bundled = bundled / PERSONA_SNAPSHOT_FILENAME

    if fetched_persona is not None:
        # Re-sync the workspace snapshot to the last-known-good.  A write error
        # here must never block startup — we already have the live persona.
        try:
            _write_text_atomic(persona_ws, fetched_persona)
            log.info(
                "%s bytes=%d", PERSONA_RESYNCED_EVENT, len(fetched_persona),
                extra={"event": PERSONA_RESYNCED_EVENT, "bytes": len(fetched_persona)},
            )
        except OSError as exc:  # pragma: no cover — defensive
            log.warning("Could not persist persona snapshot: %s", exc)
        return PersonaOverlayResult(source="superpos", persona=fetched_persona)

    # Fetch failed — degrade to snapshot (workspace preferred, bundled floor).
    snapshot = _read_text(persona_ws)
    source = "snapshot_workspace"
    if snapshot is None:
        snapshot = _read_text(persona_bundled)
        source = "snapshot_bundled" if snapshot is not None else "none"

    log.warning(
        "%s falling back to persona snapshot source=%s (agent still starts)",
        PERSONA_FETCH_FAILED_EVENT, source,
        extra={"event": PERSONA_FETCH_FAILED_EVENT, "source": source},
    )
    return PersonaOverlayResult(fetch_failed=True, source=source, persona=snapshot)


# ── Memory (read-side doubling) ──────────────────────────────────────


class MemoryFetchUnavailable(RuntimeError):
    """The Superpos MEMORY read could not reach the API (a genuine outage).

    ``fetch_fn`` (see :func:`read_memory`) must raise this — or any exception,
    which is treated the same — to signal a *transport / API error*, as opposed
    to a **reachable but empty** MEMORY document.  Only an outage falls back to
    the snapshot; a reachable-empty document clears the snapshot/cache and
    yields **no** injection.

    Without this distinction, ``fetch_fn`` returning ``None`` is ambiguous (the
    real :func:`sub_agent_sync.fetch_persona_memory` returns ``None`` for *both*
    an outage and a cleared/blank document), so a cleared MEMORY would keep
    serving — and injecting — the stale snapshot.
    """


@dataclass
class MemoryReadResult:
    """Outcome of :func:`read_memory`.

    ``source`` ∈ ``superpos`` / ``superpos_empty`` / ``cache`` /
    ``snapshot_workspace`` / ``snapshot_bundled`` / ``none``.  ``fetch_failed``
    is True only when we had to fall back to a snapshot because Superpos was
    unreachable (a genuine outage), never for a reachable-empty document.
    """

    source: str = "none"
    content: str | None = None
    fetch_failed: bool = False


def _read_cache_ts(meta_path: Path) -> float | None:
    """Return the cached fetch timestamp, or ``None`` if absent / malformed."""
    raw = _read_text(meta_path)
    if raw is None:
        return None
    try:
        ts = json.loads(raw).get("fetched_at")
        return float(ts) if ts is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _clear_memory_snapshot(mem_ws: Path, meta: Path, *, now: Callable[[], float]) -> None:
    """Clear the workspace MEMORY snapshot + cache after a reachable-empty read.

    A cleared/blank live MEMORY must *stop* serving the stale snapshot, so we
    remove the snapshot file and refresh the cache timestamp (the cache now
    legitimately means "reachable, empty").  Errors here never block the read —
    worst case the next read re-fetches.
    """
    try:
        mem_ws.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover — defensive
        log.warning("Could not clear memory snapshot: %s", exc)
    try:
        _write_text_atomic(meta, json.dumps({"fetched_at": now()}))
    except OSError as exc:  # pragma: no cover — defensive
        log.warning("Could not persist memory cache meta: %s", exc)


def read_memory(
    fetch_fn: Callable[[], str | None],
    *,
    snapshot_dir: str,
    bundled_dir: str | None = None,
    env: dict[str, str] | None = None,
    ttl_seconds: float = DEFAULT_MEMORY_TTL_SECONDS,
    now: Callable[[], float] = time.time,
) -> MemoryReadResult:
    """Read the MEMORY document, preferring Superpos with a TTL cache.

    ``fetch_fn`` is a zero-arg callable that returns the Superpos MEMORY text
    (e.g. wrapping ``GET /api/v1/persona/documents/MEMORY``).  Its return value
    disambiguates *reachable* from *outage*:

    - **Reachable** → return the document text.  A non-empty string is injected
      and re-synced to the snapshot.  An **empty / blank string or ``None``**
      means a reachable-but-empty (e.g. cleared) document: the workspace
      snapshot + cache are cleared and **no** content is injected
      (``source="superpos_empty"``, ``fetch_failed=False``).
    - **Outage** → *raise* :class:`MemoryFetchUnavailable` (or any exception).
      Only then do we fall back to the snapshot.

    Resolution:

    1. **Flag OFF** → pure passthrough of ``fetch_fn()`` (no cache, no snapshot).
    2. **Cache fresh** (within ``ttl_seconds``) → serve the workspace snapshot
       without calling ``fetch_fn`` (``source="cache"``).
    3. **Cache stale / absent** → call ``fetch_fn``; on a reachable non-empty
       read re-sync the workspace snapshot + cache timestamp
       (``source="superpos"``); on a reachable-empty read clear the snapshot +
       cache (``source="superpos_empty"``, no injection).
    4. **Superpos down** (``fetch_fn`` raised) → serve the workspace snapshot,
       else the bundled snapshot — the *read-only default rules*
       (``fetch_failed=True``).
    """
    if not feature_enabled(env):
        try:
            content = fetch_fn()
        except Exception:  # noqa: BLE001 — passthrough mode mirrors today
            content = None
        return MemoryReadResult(
            source="superpos" if content is not None else "none", content=content
        )

    ws = Path(snapshot_dir)
    mem_ws = ws / MEMORY_SNAPSHOT_FILENAME
    meta = ws / MEMORY_CACHE_META_FILENAME

    cached_at = _read_cache_ts(meta)
    if cached_at is not None and (now() - cached_at) < ttl_seconds:
        cached = _read_text(mem_ws)
        if cached is not None:
            return MemoryReadResult(source="cache", content=cached)

    try:
        fetched = fetch_fn()
    except Exception as exc:  # noqa: BLE001 — outage: isolate + fall back
        log.warning(
            "%s error=%s", MEMORY_FETCH_FAILED_EVENT, exc,
            extra={"event": MEMORY_FETCH_FAILED_EVENT, "error": str(exc)},
        )
        # Outage — serve the read-only snapshot (workspace preferred, bundled).
        snap = _read_text(mem_ws)
        source = "snapshot_workspace"
        if snap is None:
            bundled = Path(bundled_dir) if bundled_dir else bundled_snapshot_dir()
            snap = _read_text(bundled / MEMORY_SNAPSHOT_FILENAME)
            source = "snapshot_bundled" if snap is not None else "none"

        log.warning(
            "%s serving memory snapshot source=%s (read-only)",
            MEMORY_FETCH_FAILED_EVENT, source,
            extra={"event": MEMORY_FETCH_FAILED_EVENT, "source": source},
        )
        return MemoryReadResult(source=source, content=snap, fetch_failed=True)

    # Reachable.  Distinguish a real document from a cleared / blank one.
    if fetched is None or not fetched.strip():
        # Reachable-empty (e.g. user cleared MEMORY) — clear the snapshot/cache
        # so we stop injecting stale content, and inject nothing.
        _clear_memory_snapshot(mem_ws, meta, now=now)
        return MemoryReadResult(source="superpos_empty", content=None)

    try:
        _write_text_atomic(mem_ws, fetched)
        _write_text_atomic(meta, json.dumps({"fetched_at": now()}))
    except OSError as exc:  # pragma: no cover — defensive
        log.warning("Could not persist memory snapshot: %s", exc)
    return MemoryReadResult(source="superpos", content=fetched)


# ── Memory (write-side: Superpos-only, no silent fallback) ───────────


class MemoryWriteUnavailable(RuntimeError):
    """A persona-memory write could not reach Superpos.

    Raised by :func:`write_memory` so the failure is **loud**.  There is
    deliberately no agent-local fallback: degraded mode is read-only by design,
    exactly as the modules rollback is read-side only.  Writing a local copy
    would double-write the rule and diverge the two layers.
    """


def write_memory(write_fn: Callable[[], object]) -> object:
    """Run a Superpos memory write, surfacing an outage loudly.

    ``write_fn`` performs the actual ``PATCH /api/v1/persona/memory``.  On any
    failure we log a structured record and re-raise as
    :class:`MemoryWriteUnavailable` — never silently fall back to a local
    snapshot.  Returns ``write_fn``'s result on success.
    """
    try:
        return write_fn()
    except Exception as exc:  # noqa: BLE001 — convert to a loud, typed failure
        log.warning(
            "%s error=%s (no local fallback — degraded mode is read-only)",
            MEMORY_WRITE_NO_FALLBACK_EVENT, exc,
            extra={"event": MEMORY_WRITE_NO_FALLBACK_EVENT, "error": str(exc)},
        )
        raise MemoryWriteUnavailable(str(exc)) from exc


__all__ = [
    "FEATURE_FLAG_ENV",
    "PERSONA_SNAPSHOT_FILENAME",
    "MEMORY_SNAPSHOT_FILENAME",
    "MEMORY_CACHE_META_FILENAME",
    "DEFAULT_MEMORY_TTL_SECONDS",
    "PERSONA_RESYNCED_EVENT",
    "PERSONA_FETCH_FAILED_EVENT",
    "MEMORY_FETCH_FAILED_EVENT",
    "MEMORY_WRITE_NO_FALLBACK_EVENT",
    "MemoryFetchUnavailable",
    "MemoryReadResult",
    "MemoryWriteUnavailable",
    "PersonaOverlayResult",
    "apply_persona_overlay",
    "bundled_snapshot_dir",
    "feature_enabled",
    "read_memory",
    "write_memory",
]
