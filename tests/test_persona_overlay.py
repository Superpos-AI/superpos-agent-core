"""Tests for persona + memory doubling (AG-10, issue #193).

Mirrors the failure-mode coverage of ``test_registry_overlay.py``: flag-off is a
no-op, fetch failure degrades to the snapshot, a reachable fetch re-syncs the
workspace snapshot, and memory writes fail loudly with no local fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from superpos_agent_core.persona_overlay import (
    FEATURE_FLAG_ENV,
    MEMORY_CACHE_META_FILENAME,
    MEMORY_SNAPSHOT_FILENAME,
    PERSONA_FETCH_FAILED_EVENT,
    PERSONA_SNAPSHOT_FILENAME,
    MemoryFetchUnavailable,
    MemoryWriteUnavailable,
    apply_persona_overlay,
    feature_enabled,
    read_memory,
    write_memory,
)


def _outage():
    """A ``fetch_fn`` that signals a genuine Superpos outage (transport/API)."""
    raise MemoryFetchUnavailable("SUPERPOS_BASE_URL unreachable")


# ── helpers ──────────────────────────────────────────────────────────


def _bundled(tmp_path: Path, persona: str | None = None, memory: str | None = None) -> Path:
    """Create a bundled snapshot dir with optional persona/memory floor files."""
    d = tmp_path / "bundled"
    d.mkdir(parents=True, exist_ok=True)
    if persona is not None:
        (d / PERSONA_SNAPSHOT_FILENAME).write_text(persona, encoding="utf-8")
    if memory is not None:
        (d / MEMORY_SNAPSHOT_FILENAME).write_text(memory, encoding="utf-8")
    return d


# ── feature flag ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "on", "anything"])
def test_flag_enabled_by_default_and_truthy(value):
    assert feature_enabled({FEATURE_FLAG_ENV: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF ", "False"])
def test_flag_disabled_by_explicit_falsey(value):
    assert feature_enabled({FEATURE_FLAG_ENV: value}) is False


def test_flag_unset_defaults_on():
    assert feature_enabled({}) is True


# ── persona overlay ──────────────────────────────────────────────────


def test_flag_off_is_passthrough_noop(tmp_path: Path):
    """Flag OFF → persona passes through untouched, no snapshot file written."""
    ws = tmp_path / "ws"
    bundled = _bundled(tmp_path, persona="BUNDLED")

    result = apply_persona_overlay(
        "LIVE PERSONA",
        snapshot_dir=str(ws),
        bundled_dir=str(bundled),
        env={FEATURE_FLAG_ENV: "off"},
    )

    assert result.skipped is True
    assert result.persona == "LIVE PERSONA"
    # No snapshot IO at all in the rollback path.
    assert not (ws / PERSONA_SNAPSHOT_FILENAME).exists()


def test_fetch_success_resyncs_workspace_snapshot(tmp_path: Path):
    """Superpos reachable → workspace snapshot rewritten to the live persona."""
    ws = tmp_path / "ws"
    result = apply_persona_overlay(
        "FRESH FROM SUPERPOS",
        snapshot_dir=str(ws),
        bundled_dir=str(_bundled(tmp_path, persona="BUNDLED")),
        env={FEATURE_FLAG_ENV: "on"},
    )

    assert result.source == "superpos"
    assert result.persona == "FRESH FROM SUPERPOS"
    assert (ws / PERSONA_SNAPSHOT_FILENAME).read_text() == "FRESH FROM SUPERPOS"


def test_fetch_fail_falls_back_to_workspace_snapshot(tmp_path: Path, caplog):
    """fetched=None with a prior workspace snapshot → workspace snapshot served."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / PERSONA_SNAPSHOT_FILENAME).write_text("LAST KNOWN GOOD", encoding="utf-8")

    with caplog.at_level("WARNING"):
        result = apply_persona_overlay(
            None,
            snapshot_dir=str(ws),
            bundled_dir=str(_bundled(tmp_path, persona="BUNDLED")),
            env={FEATURE_FLAG_ENV: "on"},
        )

    assert result.fetch_failed is True
    assert result.source == "snapshot_workspace"
    assert result.persona == "LAST KNOWN GOOD"
    assert any(PERSONA_FETCH_FAILED_EVENT in r.message for r in caplog.records)


def test_fetch_fail_falls_back_to_bundled_floor(tmp_path: Path):
    """No workspace snapshot yet → bundled floor served (never-online agent)."""
    ws = tmp_path / "ws"  # absent
    result = apply_persona_overlay(
        None,
        snapshot_dir=str(ws),
        bundled_dir=str(_bundled(tmp_path, persona="BUNDLED FLOOR")),
        env={FEATURE_FLAG_ENV: "on"},
    )

    assert result.fetch_failed is True
    assert result.source == "snapshot_bundled"
    assert result.persona == "BUNDLED FLOOR"


def test_workspace_snapshot_preferred_over_bundled(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / PERSONA_SNAPSHOT_FILENAME).write_text("WORKSPACE WINS", encoding="utf-8")
    result = apply_persona_overlay(
        None,
        snapshot_dir=str(ws),
        bundled_dir=str(_bundled(tmp_path, persona="BUNDLED")),
        env={FEATURE_FLAG_ENV: "on"},
    )
    assert result.source == "snapshot_workspace"
    assert result.persona == "WORKSPACE WINS"


def test_fetch_fail_no_snapshot_anywhere_returns_none(tmp_path: Path):
    """Outage on a fresh agent with no bundled file → None, but no crash."""
    result = apply_persona_overlay(
        None,
        snapshot_dir=str(tmp_path / "ws"),
        bundled_dir=str(tmp_path / "empty-bundled"),
        env={FEATURE_FLAG_ENV: "on"},
    )
    assert result.fetch_failed is True
    assert result.source == "none"
    assert result.persona is None


def test_recovery_resyncs_after_outage(tmp_path: Path):
    """Outage serves snapshot → recovery rewrites the workspace snapshot."""
    ws = tmp_path / "ws"
    bundled = _bundled(tmp_path, persona="BUNDLED")
    env = {FEATURE_FLAG_ENV: "on"}

    # 1. Outage — bundled floor served, nothing re-synced yet.
    r1 = apply_persona_overlay(None, snapshot_dir=str(ws), bundled_dir=str(bundled), env=env)
    assert r1.source == "snapshot_bundled"

    # 2. Recovery — workspace snapshot now reflects the live persona.
    r2 = apply_persona_overlay("RECOVERED", snapshot_dir=str(ws), bundled_dir=str(bundled), env=env)
    assert r2.source == "superpos"
    assert (ws / PERSONA_SNAPSHOT_FILENAME).read_text() == "RECOVERED"

    # 3. Next outage now serves the re-synced workspace snapshot, not bundled.
    r3 = apply_persona_overlay(None, snapshot_dir=str(ws), bundled_dir=str(bundled), env=env)
    assert r3.source == "snapshot_workspace"
    assert r3.persona == "RECOVERED"


# ── memory read ──────────────────────────────────────────────────────


def test_memory_read_prefers_superpos_and_resyncs(tmp_path: Path):
    ws = tmp_path / "ws"
    result = read_memory(
        lambda: "LIVE MEMORY",
        snapshot_dir=str(ws),
        bundled_dir=str(_bundled(tmp_path, memory="BUNDLED MEM")),
        env={FEATURE_FLAG_ENV: "on"},
        now=lambda: 1000.0,
    )
    assert result.source == "superpos"
    assert result.content == "LIVE MEMORY"
    assert (ws / MEMORY_SNAPSHOT_FILENAME).read_text() == "LIVE MEMORY"


def test_memory_read_uses_cache_within_ttl(tmp_path: Path):
    """A fresh cache short-circuits the fetch (fetch_fn must not be called)."""
    ws = tmp_path / "ws"
    bundled = _bundled(tmp_path, memory="BUNDLED MEM")
    env = {FEATURE_FLAG_ENV: "on"}

    # Prime the cache at t=1000.
    read_memory(lambda: "CACHED", snapshot_dir=str(ws), bundled_dir=str(bundled),
                env=env, now=lambda: 1000.0, ttl_seconds=300)

    def _boom():
        raise AssertionError("fetch_fn should not be called within TTL")

    # t=1100 is within the 300s TTL → served from cache, fetch_fn untouched.
    result = read_memory(_boom, snapshot_dir=str(ws), bundled_dir=str(bundled),
                         env=env, now=lambda: 1100.0, ttl_seconds=300)
    assert result.source == "cache"
    assert result.content == "CACHED"


def test_memory_read_refetches_after_ttl(tmp_path: Path):
    ws = tmp_path / "ws"
    bundled = _bundled(tmp_path, memory="BUNDLED MEM")
    env = {FEATURE_FLAG_ENV: "on"}
    read_memory(lambda: "OLD", snapshot_dir=str(ws), bundled_dir=str(bundled),
                env=env, now=lambda: 1000.0, ttl_seconds=300)
    # t=2000 is past TTL → re-fetch.
    result = read_memory(lambda: "NEW", snapshot_dir=str(ws), bundled_dir=str(bundled),
                         env=env, now=lambda: 2000.0, ttl_seconds=300)
    assert result.source == "superpos"
    assert result.content == "NEW"


def test_memory_read_outage_serves_snapshot_readonly(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / MEMORY_SNAPSHOT_FILENAME).write_text("LKG MEMORY", encoding="utf-8")
    result = read_memory(
        _outage,  # fetch_fn raises → genuine outage
        snapshot_dir=str(ws),
        bundled_dir=str(_bundled(tmp_path, memory="BUNDLED MEM")),
        env={FEATURE_FLAG_ENV: "on"},
        now=lambda: 5000.0,
    )
    assert result.fetch_failed is True
    assert result.source == "snapshot_workspace"
    assert result.content == "LKG MEMORY"
    # Outage must NOT clear the workspace snapshot.
    assert (ws / MEMORY_SNAPSHOT_FILENAME).read_text() == "LKG MEMORY"


def test_memory_read_outage_falls_back_to_bundled(tmp_path: Path):
    result = read_memory(
        _outage,
        snapshot_dir=str(tmp_path / "ws"),
        bundled_dir=str(_bundled(tmp_path, memory="BUNDLED DEFAULTS")),
        env={FEATURE_FLAG_ENV: "on"},
        now=lambda: 1.0,
    )
    assert result.fetch_failed is True
    assert result.source == "snapshot_bundled"
    assert result.content == "BUNDLED DEFAULTS"


def test_memory_read_reachable_empty_clears_stale_snapshot(tmp_path: Path):
    """Regression: old snapshot present + live MEMORY reachable-but-empty →
    NO injection AND the stale snapshot is cleared (not served)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # An old workspace snapshot + cache from a previous successful fetch.
    (ws / MEMORY_SNAPSHOT_FILENAME).write_text("OLD STALE MEMORY", encoding="utf-8")
    (ws / MEMORY_CACHE_META_FILENAME).write_text(
        '{"fetched_at": 1000.0}', encoding="utf-8"
    )

    # ttl_seconds=0 forces a fresh fetch; the live document is now empty (user
    # cleared MEMORY) — fetch_fn returns None to mean "reachable, empty".
    result = read_memory(
        lambda: None,
        snapshot_dir=str(ws),
        bundled_dir=str(_bundled(tmp_path, memory="BUNDLED MEM")),
        env={FEATURE_FLAG_ENV: "on"},
        now=lambda: 2000.0,
        ttl_seconds=0,
    )

    # No injection, and it is NOT a snapshot fallback.
    assert result.fetch_failed is False
    assert result.source == "superpos_empty"
    assert result.content is None
    # The stale workspace snapshot must be gone so it can't be served again.
    assert not (ws / MEMORY_SNAPSHOT_FILENAME).exists()


def test_memory_read_reachable_blank_string_clears_snapshot(tmp_path: Path):
    """A reachable whitespace-only document is also 'empty' → clears + no inject."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / MEMORY_SNAPSHOT_FILENAME).write_text("OLD", encoding="utf-8")

    result = read_memory(
        lambda: "   \n  ",
        snapshot_dir=str(ws),
        bundled_dir=str(_bundled(tmp_path, memory="BUNDLED")),
        env={FEATURE_FLAG_ENV: "on"},
        now=lambda: 10.0,
        ttl_seconds=0,
    )
    assert result.fetch_failed is False
    assert result.source == "superpos_empty"
    assert result.content is None
    assert not (ws / MEMORY_SNAPSHOT_FILENAME).exists()


def test_memory_read_flag_off_is_passthrough(tmp_path: Path):
    ws = tmp_path / "ws"
    result = read_memory(
        lambda: "LIVE",
        snapshot_dir=str(ws),
        bundled_dir=str(_bundled(tmp_path, memory="BUNDLED")),
        env={FEATURE_FLAG_ENV: "off"},
    )
    assert result.source == "superpos"
    assert result.content == "LIVE"
    # No cache/snapshot IO in passthrough mode.
    assert not (ws / MEMORY_SNAPSHOT_FILENAME).exists()


# ── memory write (Superpos-only, no silent fallback) ─────────────────


def test_memory_write_success_passes_through():
    assert write_memory(lambda: {"ok": True}) == {"ok": True}


def test_memory_write_outage_raises_loudly_no_local_file(tmp_path: Path, caplog):
    """Superpos down at write time → raises; never writes an agent-local file."""
    ws = tmp_path / "ws"

    def _failing_write():
        raise ConnectionError("SUPERPOS_BASE_URL unreachable")

    with caplog.at_level("WARNING"):
        with pytest.raises(MemoryWriteUnavailable):
            write_memory(_failing_write)

    # No silent fallback: nothing written to the snapshot dir.
    assert not ws.exists() or not any(ws.iterdir())
