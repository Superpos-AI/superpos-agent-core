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


def chat_key(chat_id: int | str, thread_id: int | None = None) -> str:
    """Canonical key for per-conversation state (sessions, /stop tracking).

    A Telegram forum topic is an independent conversation, so messages
    carrying a ``thread_id`` get a composite ``"{chat_id}:{thread_id}"``
    key.  Plain chats keep the bare ``str(chat_id)`` so existing stored
    sessions keep resolving after an upgrade.
    """
    if thread_id is not None:
        return f"{chat_id}:{thread_id}"
    return str(chat_id)


@dataclass
class ExecutionRequest:
    """A single unit of work routed through the executor queue."""

    prompt: str
    chat_id: int | str
    source: str  # "telegram" | "superpos"
    superpos_task_id: str | None = None
    branch: str | None = None
    image_paths: list[str] | None = None
    # Telegram forum topic the request originated from (message_thread_id).
    # None for DMs, plain groups, and a forum's General topic.
    thread_id: int | None = None

    @property
    def chat_key(self) -> str:
        """Conversation-state key — see :func:`chat_key`."""
        return chat_key(self.chat_id, self.thread_id)


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
        # Per-chat asyncio.Task tracking for /stop.  Subclasses call
        # ``_track_chat_task`` once they have the running task in hand;
        # ``cancel_chat`` walks this map.  Keyed by ``str(chat_id)`` so
        # int/str keys interop with Telegram's int chat_ids and Superpos's
        # string ones.
        self._chat_tasks: dict[str, set[asyncio.Task]] = {}

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

    def model_info(self) -> dict[str, str] | None:
        """Report the agent's *current* LLM model/effort for the heartbeat.

        The poller forwards this to Superpos on every heartbeat so the
        dashboard reflects live model state — including mid-session
        ``/model`` / ``/effort`` switches.  Default returns ``None`` so
        agents without a tunable model send nothing (back-compat).  Agents
        backed by a ``RuntimeConfig`` override to return
        ``{"model": ..., "effort": ...}``.
        """
        return None

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

    # ── /stop support ─────────────────────────────────────────────────

    def _track_chat_task(
        self, chat_id: int | str, task: asyncio.Task,
    ) -> None:
        """Register an in-flight asyncio.Task so ``cancel_chat`` can find it.

        Subclasses should call this from ``_execute`` (or wherever the
        per-request worker is spawned) right after they create the task,
        then trust the auto-removal on done.  Calling more than once for
        the same chat is fine — a chat with parallel work (e.g. branch-
        scoped tasks) gets a set of tasks tracked.
        """
        key = str(chat_id)
        bucket = self._chat_tasks.setdefault(key, set())
        bucket.add(task)
        task.add_done_callback(lambda _t: self._untrack_chat_task(key, _t))

    def _untrack_chat_task(self, chat_key: str, task: asyncio.Task) -> None:
        bucket = self._chat_tasks.get(chat_key)
        if not bucket:
            return
        bucket.discard(task)
        if not bucket:
            self._chat_tasks.pop(chat_key, None)

    def cancel_chat(self, chat_id: int | str) -> int:
        """Cancel every in-flight task tracked for ``chat_id``.

        Returns the number of tasks signalled (which may be 0 if nothing
        was running).  Subclasses that need richer behaviour (kill a
        subprocess, send a Telegram "stopped" message, mark the Superpos
        task as failed) should override and call ``super().cancel_chat``
        to keep the asyncio cancellation path uniform.

        Also resolves any pending interactive question
        (:func:`~.ask_user.ask_user_question`) for this chat/topic so a
        ``/stop`` mid-question doesn't leak the awaiting Future — the parked
        ``ask_user_question`` coroutine sees the cancellation, cleans up its
        Telegram message, and returns a no-response result.  Counted toward
        the return value so ``/stop`` reports it did something even when the
        only outstanding work is a parked question.
        """
        # Local import avoids a module-load cycle (ask_user imports executor).
        from .ask_user import PENDING_QUESTIONS

        cancelled = PENDING_QUESTIONS.cancel_chat(chat_id)
        bucket = self._chat_tasks.get(str(chat_id))
        if not bucket:
            return cancelled
        for task in list(bucket):
            if not task.done():
                task.cancel()
                cancelled += 1
        return cancelled
