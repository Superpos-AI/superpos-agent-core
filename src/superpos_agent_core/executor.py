"""Executor contract — the seam between core and per-agent implementations.

Every concrete agent (Claude, Codex, Gemini, Qwen, …) ships a subclass of
:class:`Executor` that knows how to drive its specific LLM CLI/SDK.  Core
modules (``superpos_poller``, ``telegram_bot``, ``telegram_streamer``) only
interact with the abstract surface defined here, so adding a new agent
means writing one executor — not re-porting the rest of the runtime.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass


@dataclass
class ExecutionRequest:
    """A single unit of work routed through the executor queue."""

    prompt: str
    chat_id: int | str
    source: str  # "telegram" | "superpos"
    superpos_task_id: str | None = None
    branch: str | None = None
    image_paths: list[str] | None = None


class Executor(abc.ABC):
    """Abstract base for per-agent LLM executors.

    Subclasses MUST call ``super().__init__(max_parallel=…)`` so the queue,
    in-flight set, and active counter are initialized.  Subclasses then
    own the consumer loop (``run``) and the actual LLM invocation.
    """

    def __init__(self, max_parallel: int = 1) -> None:
        self.queue: asyncio.Queue[ExecutionRequest] = asyncio.Queue()
        self._in_flight_superpos_tasks: set[str] = set()
        self._max_parallel = max_parallel
        self._active_count = 0

    # ── Abstract: must be implemented per agent ────────────────────────

    @abc.abstractmethod
    async def run(self) -> None:
        """Consume the queue forever, dispatching requests to the LLM."""

    @abc.abstractmethod
    def update_persona(self, prompt: str | None, version: int | None = None) -> None:
        """Replace the agent's persona/system prompt.

        ``version`` is informational — agents that track persona versions
        (Claude) can persist it; agents that don't (Codex, Gemini) may ignore.
        """

    @abc.abstractmethod
    def clear_session(self, chat_id: int | str) -> None:
        """Drop any cached conversation state for this chat."""

    # ── Optional: default implementations agents can override ──────────

    async def preflight(self) -> None:
        """Verify auth/CLI installation before starting the main loop.

        Default no-op.  Agents should override to fail fast on bad creds.
        """
        return None

    async def run_background(
        self,
        task_id: str,
        prompt: str,
        task_type: str = "dream",
        timeout_seconds: int = 300,
    ) -> None:
        """Fire-and-forget execution for housekeeping tasks (dream, knowledge_fillin).

        Default raises NotImplementedError — agents that want background work
        must override.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement background tasks"
        )

    def cleanup_stale_sessions(self, max_age_hours: int = 24) -> dict[str, int]:
        """Delete LLM-specific stale session artifacts; return stats.

        Returned dict should contain (at minimum) keys: ``projects``,
        ``session_env``, ``bytes_freed``.  Default returns zeros so the
        ``/cleanup`` Telegram command works on agents without a
        per-session disk footprint.
        """
        return {"projects": 0, "session_env": 0, "bytes_freed": 0}

    # ── Concrete: shared bookkeeping ───────────────────────────────────

    @property
    def pending(self) -> int:
        return self.queue.qsize()

    @property
    def is_busy(self) -> bool:
        return self._active_count > 0

    @property
    def has_free_slots(self) -> bool:
        """True if more concurrent work can be accepted.

        Uses the in-flight task set rather than ``qsize()`` / ``_active_count``
        because a task can be claimed but waiting for the semaphore —
        ``qsize()`` is 0 then, but the slot is taken.
        """
        return len(self._in_flight_superpos_tasks) < self._max_parallel

    def add_superpos_task(self, task_id: str) -> None:
        self._in_flight_superpos_tasks.add(task_id)

    def remove_superpos_task(self, task_id: str) -> None:
        self._in_flight_superpos_tasks.discard(task_id)

    def has_superpos_task(self, task_id: str) -> bool:
        return task_id in self._in_flight_superpos_tasks
