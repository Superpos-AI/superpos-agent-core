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
import time

import httpx

from .superpos_client import SuperposClient

log = logging.getLogger(__name__)


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
        try:
            await client.update_progress(task_id, progress)
            last_success = time.monotonic()
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
