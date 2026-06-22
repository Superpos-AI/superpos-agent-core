"""Tests for superpos_poller — persona overlay in the refresh path (AG-10, PR #53).

Regression the reviewer explicitly requested: a *changed* persona poll whose
``get_persona_assembled`` returns ``None`` (a transient assembled-fetch outage)
must NOT push ``None`` into the live executor — it must fall back to the
workspace ``.persona-snapshot`` and leave the tracked version un-advanced so the
next reachable poll re-fetches.  A reachable fetch must re-sync that snapshot.

Reuses the harness style from ``test_superpos_poller_resync.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from superpos_agent_core import superpos_poller as poller
from superpos_agent_core.persona_overlay import (
    PERSONA_SNAPSHOT_FILENAME,
    PersonaFetchUnavailable,
)


class _FakeClient:
    """SuperposClient stub whose assembled persona is configurable."""

    def __init__(self, persona_versions, assembled):
        self._persona_versions = list(persona_versions)
        self._assembled = assembled
        self.heartbeats = 0

    async def heartbeat(self, *, model=None, effort=None):
        self.heartbeats += 1

    async def get_persona_version(self, **kwargs):
        if not self._persona_versions:
            raise RuntimeError("no more scripted versions")
        return {"data": self._persona_versions.pop(0)}

    async def get_persona_assembled(self):
        # An exception value models a genuine outage (the real client raises
        # PersonaFetchUnavailable); any other value is a reachable result.
        if isinstance(self._assembled, BaseException) or (
            isinstance(self._assembled, type)
            and issubclass(self._assembled, BaseException)
        ):
            raise self._assembled
        return self._assembled

    async def poll_tasks(self):
        return []


class _FakeExecutor:
    pending = 0
    has_free_slots = True

    def __init__(self):
        self.persona_updates = []
        self._tasks = set()

    def update_persona(self, prompt, version=None):
        self.persona_updates.append((prompt, version))

    def model_info(self):
        return None

    def has_superpos_task(self, task_id):
        return task_id in self._tasks

    def add_superpos_task(self, task_id):
        self._tasks.add(task_id)


def _make_config(working_dir: Path):
    class _Config:
        superpos_base_url = "http://fake"
        superpos_api_token = "tok"
        superpos_poll_interval = 0
        executor_working_dir = str(working_dir)
        executor_kind = "claude"
        modules_dir = None
        telegram_chat_id = "0"
        executor_max_parallel = 1

    return _Config()


async def _run_until(client, executor, config, *, until):
    """Run the poller until ``until()`` is true or the script is exhausted."""
    task = asyncio.create_task(
        poller.run_superpos_poller(client, executor, config)
    )
    for _ in range(200):
        await asyncio.sleep(0)
        if until():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_assembled_fetch_none_falls_back_to_snapshot(tmp_path, monkeypatch):
    """Changed persona + assembled fetch None → serve the snapshot, not None."""
    monkeypatch.setattr(poller, "_resync_sub_agents", lambda *a, **k: None)

    snapshot_dir = tmp_path / ".persona-snapshot"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / PERSONA_SNAPSHOT_FILENAME).write_text(
        "last-known-good-persona", encoding="utf-8"
    )

    client = _FakeClient(
        persona_versions=[
            {"changed": True, "version": 7},
            {"changed": False, "version": 7},
        ],
        assembled=PersonaFetchUnavailable("outage"),  # genuine outage → raises
    )
    executor = _FakeExecutor()
    config = _make_config(tmp_path)

    await _run_until(
        client, executor, config, until=lambda: bool(executor.persona_updates)
    )

    assert executor.persona_updates, "executor.update_persona was never called"
    prompt, _version = executor.persona_updates[0]
    # Served the snapshot content, never None.
    assert prompt == "last-known-good-persona"


@pytest.mark.asyncio
async def test_assembled_fetch_none_does_not_advance_version(tmp_path, monkeypatch):
    """On a fetch outage the tracked persona_version must NOT advance.

    Asserted indirectly: a *subsequent* poll reporting the same server version
    re-detects it as changed (because the failed poll left persona_version
    un-advanced), so the assembled fetch is retried — observable as a second
    update_persona call.
    """
    monkeypatch.setattr(poller, "_resync_sub_agents", lambda *a, **k: None)

    snapshot_dir = tmp_path / ".persona-snapshot"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / PERSONA_SNAPSHOT_FILENAME).write_text(
        "snap", encoding="utf-8"
    )

    client = _FakeClient(
        persona_versions=[
            {"changed": True, "version": 9},
            {"changed": False, "version": 9},  # same version, still re-detected
        ],
        assembled=PersonaFetchUnavailable("outage"),
    )
    executor = _FakeExecutor()
    config = _make_config(tmp_path)

    await _run_until(
        client,
        executor,
        config,
        until=lambda: not client._persona_versions and len(executor.persona_updates) >= 2,
    )

    # Both polls re-fetched because version stayed un-advanced after the outage.
    assert len(executor.persona_updates) >= 2


@pytest.mark.asyncio
async def test_assembled_fetch_success_resyncs_snapshot(tmp_path, monkeypatch):
    """A reachable assembled fetch is pushed to the executor and re-syncs snapshot."""
    monkeypatch.setattr(poller, "_resync_sub_agents", lambda *a, **k: None)

    client = _FakeClient(
        persona_versions=[
            {"changed": True, "version": 3},
            {"changed": False, "version": 3},
        ],
        assembled="fresh-assembled-persona",
    )
    executor = _FakeExecutor()
    config = _make_config(tmp_path)

    await _run_until(
        client, executor, config, until=lambda: bool(executor.persona_updates)
    )

    prompt, version = executor.persona_updates[0]
    assert prompt == "fresh-assembled-persona"
    assert version == 3

    snapshot_file = tmp_path / ".persona-snapshot" / PERSONA_SNAPSHOT_FILENAME
    assert snapshot_file.exists(), "workspace snapshot was not re-synced"
    assert snapshot_file.read_text(encoding="utf-8") == "fresh-assembled-persona"


@pytest.mark.asyncio
async def test_assembled_reachable_empty_clears_snapshot_no_resurrection(
    tmp_path, monkeypatch
):
    """Changed persona + reachable-empty assembled (operator cleared it) →
    push None to the executor, clear the stale snapshot, do NOT resurrect it.
    """
    monkeypatch.setattr(poller, "_resync_sub_agents", lambda *a, **k: None)

    snapshot_dir = tmp_path / ".persona-snapshot"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / PERSONA_SNAPSHOT_FILENAME).write_text(
        "stale-persona", encoding="utf-8"
    )

    client = _FakeClient(
        persona_versions=[
            {"changed": True, "version": 5},
            {"changed": False, "version": 5},
        ],
        assembled=None,  # reachable, no active persona (NOT an outage → no raise)
    )
    executor = _FakeExecutor()
    config = _make_config(tmp_path)

    await _run_until(
        client, executor, config, until=lambda: bool(executor.persona_updates)
    )

    prompt, version = executor.persona_updates[0]
    # No snapshot resurrection — the executor gets no persona.
    assert prompt is None
    # Reachable-empty is authoritative, so the version advances (not retried).
    assert version == 5
    # The stale snapshot was cleared so a later outage can't resurrect it.
    assert not (snapshot_dir / PERSONA_SNAPSHOT_FILENAME).exists()
