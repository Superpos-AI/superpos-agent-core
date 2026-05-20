"""Tests for the /stop cancellation infrastructure on the Executor base class.

The mechanism: subclasses call ``_track_chat_task`` after spawning the
per-request asyncio task; the base class auto-untracks on completion and
``cancel_chat`` walks the tracked map and signals ``.cancel()``.
"""

from __future__ import annotations

import asyncio

import pytest

from superpos_agent_core import Executor


class _MinimalExecutor(Executor):
    """Stub subclass — just enough to instantiate the abstract base."""

    async def run(self) -> None:
        # Tests drive cancellation directly; the consumer loop isn't exercised.
        await asyncio.sleep(0)

    def update_persona(self, prompt: str | None, version: int | None = None) -> None:
        pass

    def clear_session(self, chat_id) -> None:
        pass


def _make() -> _MinimalExecutor:
    return _MinimalExecutor(max_parallel=2)


# ── _track_chat_task / auto-untracking ───────────────────────────────────


async def test_track_chat_task_records_in_bucket():
    ex = _make()

    async def _sleep():
        await asyncio.sleep(0.5)

    task = asyncio.create_task(_sleep())
    ex._track_chat_task(123, task)

    assert "123" in ex._chat_tasks
    assert task in ex._chat_tasks["123"]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_completed_task_is_auto_untracked():
    ex = _make()

    async def _quick():
        return "done"

    task = asyncio.create_task(_quick())
    ex._track_chat_task("c1", task)
    await task
    # Yield so the done callback can run
    await asyncio.sleep(0)

    assert "c1" not in ex._chat_tasks


async def test_cancelled_task_is_auto_untracked():
    ex = _make()

    async def _forever():
        await asyncio.sleep(60)

    task = asyncio.create_task(_forever())
    ex._track_chat_task("c1", task)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)

    assert "c1" not in ex._chat_tasks


# ── cancel_chat behaviour ────────────────────────────────────────────────


async def test_cancel_chat_signals_one_running_task():
    ex = _make()
    started = asyncio.Event()

    async def _worker():
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(_worker())
    ex._track_chat_task("c1", task)
    await started.wait()

    cancelled = ex.cancel_chat("c1")
    assert cancelled == 1

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_chat_returns_zero_when_nothing_running():
    ex = _make()
    assert ex.cancel_chat("nonexistent") == 0


async def test_cancel_chat_handles_int_and_str_chat_ids_interop():
    """Telegram passes int chat_ids, Superpos passes string ones — both
    should resolve to the same tracked bucket."""
    ex = _make()

    async def _forever():
        await asyncio.sleep(60)

    task = asyncio.create_task(_forever())
    ex._track_chat_task(42, task)  # tracked under int

    cancelled = ex.cancel_chat("42")  # cancelled as str
    assert cancelled == 1

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_chat_only_cancels_target_chat():
    """A chat with running work in flight must not be affected when
    /stop fires on a different chat."""
    ex = _make()
    started_c1 = asyncio.Event()
    started_c2 = asyncio.Event()

    async def _worker(started: asyncio.Event):
        started.set()
        await asyncio.sleep(60)

    t1 = asyncio.create_task(_worker(started_c1))
    t2 = asyncio.create_task(_worker(started_c2))
    ex._track_chat_task("c1", t1)
    ex._track_chat_task("c2", t2)
    await started_c1.wait()
    await started_c2.wait()

    cancelled = ex.cancel_chat("c1")
    assert cancelled == 1
    assert t1.cancelled() or t1.cancelling() > 0
    assert not t2.done(), "the other chat's task must still be running"

    t2.cancel()
    for t in (t1, t2):
        try:
            await t
        except asyncio.CancelledError:
            pass


async def test_cancel_chat_handles_multiple_parallel_tasks_for_same_chat():
    """A single chat can have parallel work (branch-isolated tasks); /stop
    should cancel all of them, not just one."""
    ex = _make()
    started = [asyncio.Event(), asyncio.Event()]

    async def _worker(started_ev: asyncio.Event):
        started_ev.set()
        await asyncio.sleep(60)

    tasks = [
        asyncio.create_task(_worker(started[0])),
        asyncio.create_task(_worker(started[1])),
    ]
    for t in tasks:
        ex._track_chat_task("c1", t)
    await asyncio.gather(*(e.wait() for e in started))

    cancelled = ex.cancel_chat("c1")
    assert cancelled == 2
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass


async def test_cancel_chat_skips_already_done_tasks():
    """A task that finished naturally before /stop fires shouldn't count
    as ``cancelled``.  Done-callback auto-untracks first, so this case
    is normally empty — but if a stale entry lingers (race), the loop
    must not re-cancel a done task."""
    ex = _make()

    async def _quick():
        return "done"

    task = asyncio.create_task(_quick())
    # Manually track without waiting for done callback
    ex._chat_tasks["c1"] = {task}
    await task

    cancelled = ex.cancel_chat("c1")
    assert cancelled == 0
