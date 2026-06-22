"""Tests for sub_agent_sync MEMORY read routing through the AG-10 overlay (PR #53).

When ``memory_snapshot_dir`` is provided the live MEMORY read is routed through
``persona_overlay.read_memory`` so a Superpos *outage* degrades to the read-only
workspace snapshot, a reachable read re-syncs that snapshot, and a reachable but
*empty* (cleared) document clears the snapshot so stale memory stops being
injected into sub-agents.
"""

from __future__ import annotations

from superpos_agent_core import sub_agent_sync
from superpos_agent_core.persona_overlay import (
    MEMORY_SNAPSHOT_FILENAME,
    MemoryFetchUnavailable,
)
from superpos_agent_core.sub_agent_sync import sync_sub_agents


_DEFS = [{"slug": "test", "name": "Test", "version": 1, "documents": {}}]


def test_live_memory_resyncs_snapshot(tmp_path):
    """A live MEMORY value is injected AND written to the workspace snapshot."""
    sub_dir = tmp_path / "subagents"
    snap_dir = tmp_path / "snap"

    count = sync_sub_agents(
        subagents_dir=str(sub_dir),
        base_url="http://fake",
        token="fake",
        definitions=_DEFS,
        memory="live-mem",
        inject_memory=True,
        memory_snapshot_dir=str(snap_dir),
    )

    assert count == 1
    content = (sub_dir / "test.md").read_text()
    assert "live-mem" in content

    snap_file = snap_dir / MEMORY_SNAPSHOT_FILENAME
    assert snap_file.exists(), "MEMORY snapshot was not re-synced"
    assert snap_file.read_text(encoding="utf-8") == "live-mem"


def test_reachable_empty_memory_clears_stale_snapshot(tmp_path):
    """Regression: caller-provided memory=None (reachable-empty, e.g. user
    cleared MEMORY) → NO injection AND the stale snapshot is cleared, not served.
    """
    sub_dir = tmp_path / "subagents"
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir(parents=True)
    (snap_dir / MEMORY_SNAPSHOT_FILENAME).write_text("stale-mem", encoding="utf-8")

    sync_sub_agents(
        subagents_dir=str(sub_dir),
        base_url="http://fake",
        token="fake",
        definitions=_DEFS,
        memory=None,  # reachable, no memory (NOT an outage)
        inject_memory=True,
        memory_snapshot_dir=str(snap_dir),
    )

    content = (sub_dir / "test.md").read_text()
    # The stale snapshot must NOT have been injected.
    assert "stale-mem" not in content
    # And it must have been cleared so it can't be served on a later read.
    assert not (snap_dir / MEMORY_SNAPSHOT_FILENAME).exists()


def test_outage_falls_back_to_snapshot(tmp_path, monkeypatch):
    """A genuine outage (fetch_persona_memory raises) → snapshot is injected."""
    sub_dir = tmp_path / "subagents"
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir(parents=True)
    (snap_dir / MEMORY_SNAPSHOT_FILENAME).write_text("snap-mem", encoding="utf-8")

    # No bundle endpoint, definitions come from the N+1 fetch, and the MEMORY
    # fetch raises → outage path through read_memory.
    monkeypatch.setattr(sub_agent_sync, "fetch_runtime_bundle", lambda *a, **k: None)
    monkeypatch.setattr(
        sub_agent_sync, "fetch_sub_agent_definitions", lambda *a, **k: _DEFS
    )

    def _raise(*_a, **_k):
        raise MemoryFetchUnavailable("Superpos unreachable")

    monkeypatch.setattr(sub_agent_sync, "fetch_persona_memory", _raise)

    sync_sub_agents(
        subagents_dir=str(sub_dir),
        base_url="http://fake",
        token="fake",
        inject_memory=True,
        memory_snapshot_dir=str(snap_dir),
    )

    content = (sub_dir / "test.md").read_text()
    assert "snap-mem" in content
    # Outage must not clear the snapshot.
    assert (snap_dir / MEMORY_SNAPSHOT_FILENAME).exists()
