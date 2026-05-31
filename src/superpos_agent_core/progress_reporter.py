"""Background progress heartbeat for an in-flight Superpos task.

Replaces the ``_report_progress`` coroutine each per-agent executor used to
carry as its own copy.  Two correctness wins over the old per-agent code:

1. **Loud failure logging.** The old code logged failed pings at ``debug``,
   so a long run of network blips or 5xx responses left zero trace at
   INFO level — the only visible artefact was the eventual 409 once the
   server's ``progress_timeout`` had already fired.  This module logs
   every failure at ``warning`` with the underlying error type, so a
   silent claim death turns into a noisy log trail.

2. **Time-based silence detection.** Instead of only reacting to the
   server's 409, the helper tracks how long it has been since the last
   *successful* ping (monotonic clock).  Once silence exceeds
   ``silent_max_seconds`` (default 50s, comfortably under the server's
   default ``progress_timeout`` of 60s), the helper sets
   ``claim_expired`` itself.  That gives the executor a chance to abort
   and call ``fail_task`` with a meaningful "progress silent" message
   before the server reclaims the task via timeout — turning silent
   ``progress_timed_out`` dead-letters into explicit, attributable
   failures.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import httpx

from .superpos_client import SuperposClient

log = logging.getLogger(__name__)

# asyncio.timeout() was added in Python 3.11.  On 3.10 we fall back to a
# minimal contextmanager that uses asyncio.current_task().cancel() with a
# deadline callback — avoiding asyncio.wait_for() whose 3.10
# implementation has a known bug where external cancellation can be
# swallowed (https://github.com/python/cpython/issues/86296).
_HAS_ASYNCIO_TIMEOUT = sys.version_info >= (3, 11)


async def _wait_for_compat(coro, timeout: float) -> None:
    """Await *coro* with a timeout, safe against external cancellation.

    Unlike ``asyncio.wait_for`` on Python 3.10, this implementation does
    not swallow ``CancelledError`` delivered from outside.  It schedules a
    cancel callback via ``loop.call_later`` and distinguishes between
    "we timed out" (raise ``TimeoutError``) and "someone else cancelled
    us" (re-raise ``CancelledError``).
    """
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro, loop=loop)
    timed_out = False

    def _on_timeout():
        nonlocal timed_out
        timed_out = True
        task.cancel()

    handle = loop.call_later(max(timeout, 0), _on_timeout)
    try:
        return await task
    except asyncio.CancelledError:
        if timed_out:
            raise TimeoutError
        # External cancellation — clean up the inner task and propagate.
        task.cancel()
        raise
    finally:
        handle.cancel()


async def report_progress(
    client: SuperposClient,
    task_id: str,
    claim_expired: asyncio.Event,
    *,
    interval: int = 30,
    silent_max_seconds: int = 50,
) -> None:
    """Heartbeat ``PATCH /tasks/{id}/progress`` until cancelled or claim expires.

    Caller spawns this as a background task at the start of execution and
    cancels it when the inner work finishes.  Two exit paths set
    ``claim_expired`` so the caller's ``_watch_claim_expiry`` cancels the
    inner task and aborts:

    - **409 from the server** — our claim was already revoked
      (concurrent agent claimed, or server-side progress timeout fired).
    - **Silent stretch >= silent_max_seconds** — successive ``update_progress``
      calls have been failing (network, 5xx, timeout) long enough that
      the server has almost certainly killed the claim.  We give up
      rather than wait for the next attempt to come back as a 409.

    ``silent_max_seconds`` defaults to 50s, intentionally below the
    server's default 60s ``progress_timeout`` so the agent reacts first.
    Override only if a deployment has a non-default server timeout.
    """
    progress = 5
    last_success = time.monotonic()

    while True:
        # Cap each wait at the remaining silence budget so an outage is
        # detected at silent_max_seconds rather than at the next interval
        # boundary.  Without this, defaults (interval=30, threshold=50)
        # would only re-check silence at ~30s and ~60s — racing the
        # server's own 60s progress_timeout.
        budget = silent_max_seconds - (time.monotonic() - last_success)
        sleep_for = max(0.0, min(float(interval), budget))
        await asyncio.sleep(sleep_for)

        silence = time.monotonic() - last_success
        if silence >= silent_max_seconds:
            log.warning(
                "Progress silent for %.1fs on task %s (>=%ds threshold); "
                "treating claim as lost, aborting execution",
                silence, task_id, silent_max_seconds,
            )
            claim_expired.set()
            return

        progress = min(progress + 5, 95)
        # Bound the ping itself by the remaining silence budget — without
        # this, an `update_progress` call that hangs until the underlying
        # httpx timeout (default 30s) defeats the silence check entirely:
        # we'd sit inside the call until ~`silent_max_seconds + 30s`,
        # racing the server's progress_timeout the very thing this loop
        # exists to beat.
        ping_timeout = silent_max_seconds - (time.monotonic() - last_success)
        try:
            if _HAS_ASYNCIO_TIMEOUT:
                async with asyncio.timeout(ping_timeout):
                    await client.update_progress(task_id, progress)
            else:
                await _wait_for_compat(
                    client.update_progress(task_id, progress),
                    ping_timeout,
                )
            last_success = time.monotonic()
        except (asyncio.TimeoutError, TimeoutError):
            log.warning(
                "Progress update for task %s exceeded silence budget "
                "(%.1fs); next iter will abort",
                task_id, ping_timeout,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                log.warning(
                    "Claim expired for task %s (409); aborting execution", task_id,
                )
                claim_expired.set()
                return
            log.warning(
                "Progress update failed for task %s (HTTP %d)",
                task_id, e.response.status_code,
            )
        except Exception as e:
            log.warning(
                "Progress update failed for task %s (%s)",
                task_id, type(e).__name__,
            )
