"""Tests for superpos_poller — sub-agent resync trigger logic (Finding 2)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from superpos_agent_core import superpos_poller as poller


class _FakeClient:
    """Minimal SuperposClient stub for the poller loop."""

    def __init__(self, persona_versions):
        # Sequence of values returned successively by get_persona_version.
        # Each entry: dict that becomes the response under 'data'.
        self._persona_versions = list(persona_versions)
        self.heartbeats = 0

    async def heartbeat(self):
        self.heartbeats += 1

    async def get_persona_version(self, **kwargs):
        if not self._persona_versions:
            # Loop ran longer than scripted — return last value forever.
            raise RuntimeError("no more scripted versions")
        nxt = self._persona_versions.pop(0)
        return {"data": nxt}

    async def get_persona_assembled(self):
        return "assembled-persona"

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

    def has_superpos_task(self, task_id):
        return task_id in self._tasks

    def add_superpos_task(self, task_id):
        self._tasks.add(task_id)


class _FakeConfig:
    superpos_base_url = "http://fake"
    superpos_api_token = "tok"
    superpos_poll_interval = 0  # tight loop, we cancel after N iters
    executor_working_dir = "/tmp/wd"
    executor_kind = "claude"
    modules_dir = None
    telegram_chat_id = "0"
    executor_max_parallel = 1


async def _run_for_iterations(coro_fn, n: int, advance_event: asyncio.Event):
    """Run the poller, cancel after `n` poll iterations are observed."""
    task = asyncio.create_task(coro_fn())
    # Wait briefly enough for n iterations of an interval=0 loop to complete.
    # Each iteration does heartbeat → persona → poll → sleep(0).  We poll
    # the cancellation gate using the event from a sentinel scheduling.
    for _ in range(n * 20):
        await asyncio.sleep(0)
        if advance_event.is_set():
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_first_poll_with_none_version_triggers_resync(monkeypatch):
    """server_version=None on first poll must still trigger _resync_sub_agents."""
    calls = []

    def fake_resync(superpos, config):  # noqa: ARG001
        calls.append("called")

    monkeypatch.setattr(poller, "_resync_sub_agents", fake_resync)

    # One scripted response: no active persona (version=None, changed=False).
    client = _FakeClient(
        persona_versions=[
            {"changed": False, "version": None},
            {"changed": False, "version": None},  # subsequent identical poll
        ]
    )
    executor = _FakeExecutor()
    config = _FakeConfig()

    task = asyncio.create_task(
        poller.run_superpos_poller(client, executor, config)
    )
    # Let the loop process both scripted responses.
    for _ in range(50):
        await asyncio.sleep(0)
        if not client._persona_versions:
            break
    # Give one more tick so the second iteration's logic runs.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # First iteration must have triggered exactly one resync; the second
    # (unchanged null version) must NOT have triggered another.
    assert len(calls) == 1, f"expected exactly 1 resync call, got {len(calls)}"


@pytest.mark.asyncio
async def test_persona_change_triggers_resync(monkeypatch):
    """A real persona version bump still triggers resync."""
    calls = []

    def fake_resync(superpos, config):  # noqa: ARG001
        calls.append("called")

    monkeypatch.setattr(poller, "_resync_sub_agents", fake_resync)

    client = _FakeClient(
        persona_versions=[
            {"changed": False, "version": 5},  # first poll: seeds, also triggers (first observation)
            {"changed": True, "version": 6},   # change: triggers
            {"changed": False, "version": 6},  # no change: no trigger
        ]
    )
    executor = _FakeExecutor()
    config = _FakeConfig()

    task = asyncio.create_task(
        poller.run_superpos_poller(client, executor, config)
    )
    for _ in range(80):
        await asyncio.sleep(0)
        if not client._persona_versions:
            break
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) == 2, f"expected 2 resync calls (first-poll + change), got {len(calls)}"
