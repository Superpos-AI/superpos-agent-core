"""Tests for sub_agent_sync MEMORY read routing through the AG-10 overlay (PR #53).

When ``memory_snapshot_dir`` is provided the live MEMORY read is routed through
``persona_overlay.read_memory`` so a Superpos outage degrades to the read-only
workspace snapshot and a reachable read re-syncs that snapshot.
"""

from __future__ import annotations

from superpos_agent_core.persona_overlay import MEMORY_SNAPSHOT_FILENAME
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


def test_outage_falls_back_to_snapshot(tmp_path):
    """memory=None (outage) but a pre-seeded snapshot → snapshot is injected."""
    sub_dir = tmp_path / "subagents"
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir(parents=True)
    (snap_dir / MEMORY_SNAPSHOT_FILENAME).write_text("snap-mem", encoding="utf-8")

    sync_sub_agents(
        subagents_dir=str(sub_dir),
        base_url="http://fake",
        token="fake",
        definitions=_DEFS,
        memory=None,
        inject_memory=True,
        memory_snapshot_dir=str(snap_dir),
    )

    content = (sub_dir / "test.md").read_text()
    assert "snap-mem" in content
