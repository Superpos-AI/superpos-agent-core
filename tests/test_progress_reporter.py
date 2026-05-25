"""Tests for ``superpos_agent_core.progress_reporter.report_progress``.

The helper sits on a background asyncio task during execution.  Tests
drive it with a stub ``SuperposClient`` that records calls and lets each
test inject the response (success / 409 / arbitrary exception) so we can
verify the three exit paths plus the timing logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from superpos_agent_core import report_progress


class _StubClient:
    """Minimal stand-in for SuperposClient that records ``update_progress`` calls.

    Each call invokes ``handler(progress)`` so individual tests can decide
    whether to return normally, raise ``HTTPStatusError(409)``, or raise
    something else entirely.  Recording the calls separately lets us also
    assert on the cadence / progress values.
    """

    def __init__(
        self,
        handler: Callable[[int], Awaitable[Any]] | None = None,
    ) -> None:
        self.handler = handler or self._default_handler
        self.calls: list[tuple[str, int]] = []

    @staticmethod
    async def _default_handler(progress: int) -> dict[str, Any]:
        return {"ok": True, "progress": progress}

    async def update_progress(self, task_id: str, progress: int) -> Any:
        self.calls.append((task_id, progress))
        return await self.handler(progress)


def _http_409() -> httpx.HTTPStatusError:
    """Build a real 409 HTTPStatusError — same shape the SuperposClient raises."""
    request = httpx.Request("PATCH", "https://example/test")
    response = httpx.Response(409, request=request)
    return httpx.HTTPStatusError("409", request=request, response=response)


def _http_503() -> httpx.HTTPStatusError:
    request = httpx.Request("PATCH", "https://example/test")
    response = httpx.Response(503, request=request)
    return httpx.HTTPStatusError("503", request=request, response=response)


# ── happy path ──────────────────────────────────────────────────────────


async def test_pings_at_interval_and_increments_progress():
    """Healthy run: each tick increments ``progress`` by 5 and records a call."""
    client = _StubClient()
    claim_expired = asyncio.Event()

    task = asyncio.create_task(
        report_progress(
            client, "t1", claim_expired,
            interval=0,  # no real sleep — yields once and continues
            silent_max_seconds=10,
        )
    )

    # Let the coroutine run a handful of iterations, then cancel.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(client.calls) >= 3
    # Progress starts at 10 (5 + 5 first increment) and grows by 5 each
    # tick until it caps at 95.  We only assert monotonicity (never goes
    # down), because with very short intervals the helper may run
    # hundreds of ticks and saturate at the cap.
    progresses = [p for _tid, p in client.calls]
    assert progresses[0] == 10
    assert progresses[1] == 15
    assert all(p1 <= p2 for p1, p2 in zip(progresses, progresses[1:]))
    assert not claim_expired.is_set()


async def test_progress_caps_at_95():
    """The progress value caps at 95 — the server reserves 96-100 for
    completion stages, and we should never blast past that during the
    in-flight heartbeat."""
    client = _StubClient()
    claim_expired = asyncio.Event()

    task = asyncio.create_task(
        report_progress(client, "t1", claim_expired, interval=0, silent_max_seconds=999),
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert max(p for _, p in client.calls) <= 95


# ── 409 path ────────────────────────────────────────────────────────────


async def test_409_sets_claim_expired_and_returns():
    """When the server says we no longer own the task, the helper sets the
    event and exits cleanly — the executor needs that signal to abort."""

    async def handler(progress: int) -> Any:
        raise _http_409()

    client = _StubClient(handler)
    claim_expired = asyncio.Event()

    await asyncio.wait_for(
        report_progress(
            client, "t1", claim_expired,
            interval=0, silent_max_seconds=999,
        ),
        timeout=1.0,
    )

    assert claim_expired.is_set()
    assert len(client.calls) == 1  # exited after the very first 409


async def test_409_logged_as_warning(caplog):
    """Claim-expired must surface at WARNING — used by ops dashboards / on-call."""
    caplog.set_level(logging.WARNING, logger="superpos_agent_core.progress_reporter")

    async def handler(progress: int) -> Any:
        raise _http_409()

    client = _StubClient(handler)
    claim_expired = asyncio.Event()

    await report_progress(
        client, "t1", claim_expired, interval=0, silent_max_seconds=999,
    )

    assert any("Claim expired" in r.message for r in caplog.records)


# ── silence-based exit ─────────────────────────────────────────────────


async def test_silent_max_seconds_triggers_abort():
    """Codex agent failure mode: non-409 errors (network/5xx/timeouts)
    silently piling up until the server kills our claim.  The helper
    should self-terminate after ``silent_max_seconds`` of no successful
    ping, rather than waiting for the 409 to surface."""

    async def handler(progress: int) -> Any:
        raise _http_503()  # not a 409 → previously swallowed

    client = _StubClient(handler)
    claim_expired = asyncio.Event()

    # interval=0 so we attempt rapidly; silent_max_seconds is a wall-clock
    # threshold against time.monotonic(), so it still gates the exit.
    started = time.monotonic()
    await asyncio.wait_for(
        report_progress(
            client, "t1", claim_expired,
            interval=0, silent_max_seconds=0.1,
        ),
        timeout=1.0,
    )

    elapsed = time.monotonic() - started
    assert claim_expired.is_set()
    assert elapsed >= 0.1, "should not exit before silent_max_seconds elapses"
    assert len(client.calls) >= 1


async def test_silence_resets_after_successful_ping():
    """A transient failure followed by a successful ping should reset the
    silence clock — we don't want one network blip to count toward the
    abort threshold forever."""
    call_count = {"n": 0}

    async def handler(progress: int) -> Any:
        call_count["n"] += 1
        # First call fails (transient blip), subsequent calls succeed.
        if call_count["n"] == 1:
            raise _http_503()
        return {"ok": True}

    client = _StubClient(handler)
    claim_expired = asyncio.Event()

    task = asyncio.create_task(
        report_progress(
            client, "t1", claim_expired,
            interval=0,
            # Set above the time the test will actually consume so silence
            # detection doesn't fire — what we're testing is that the
            # reset-after-success means we never hit the threshold.
            silent_max_seconds=10,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not claim_expired.is_set()
    assert call_count["n"] >= 3


# ── failure logging ────────────────────────────────────────────────────


async def test_non_409_failure_logged_as_warning(caplog):
    """The old per-agent ``_report_progress`` logged non-409 failures at
    DEBUG, which is exactly why silent ``progress_timed_out`` failures
    were invisible in INFO-level production logs.  The core helper must
    log them at WARNING."""
    caplog.set_level(logging.WARNING, logger="superpos_agent_core.progress_reporter")

    async def handler(progress: int) -> Any:
        raise _http_503()

    client = _StubClient(handler)
    claim_expired = asyncio.Event()

    # Use a generous threshold so silence-detection doesn't race ahead and
    # short-circuit the test — we want at least one failed ping logged.
    task = asyncio.create_task(
        report_progress(
            client, "t1", claim_expired,
            interval=0, silent_max_seconds=10,
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("Progress update failed" in m for m in warnings), warnings


async def test_arbitrary_exception_does_not_propagate(caplog):
    """A non-HTTPStatusError exception (TimeoutException, DNS error,
    arbitrary bug) must be caught by the helper — propagating it would
    kill the background task and silently stop heartbeats."""
    caplog.set_level(logging.WARNING, logger="superpos_agent_core.progress_reporter")

    async def handler(progress: int) -> Any:
        raise RuntimeError("simulated bug")

    client = _StubClient(handler)
    claim_expired = asyncio.Event()

    task = asyncio.create_task(
        report_progress(
            client, "t1", claim_expired,
            interval=0, silent_max_seconds=10,
        )
    )
    # Let several ticks attempt and fail
    await asyncio.sleep(0.02)
    assert not task.done(), "helper must keep retrying, not die from one exception"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("RuntimeError" in m for m in warnings), warnings
