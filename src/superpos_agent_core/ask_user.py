"""Interactive agent questions over Telegram (inline keyboards).

This module is the ``superpos-agent-core`` half of the ``AskUserQuestion``
integration (see knowledge ``proposal-askuserquestion-telegram``).  An agent
that wants to ask the user a structured multiple-choice question calls
:func:`ask_user_question`, which renders one Telegram message per question
with an inline keyboard, parks on an ``asyncio.Future``, and resolves when the
user taps an option (single-select) or "Done" (multi-select).

The consumer is the Claude executor in the sibling ``superpos-claude-agent``
repo: its in-process SDK MCP tool (shadowing the native ``AskUserQuestion``)
calls this coroutine and serializes the returned dict as the tool result, so
the model resumes on the user's selection.

Wiring for the consumer
------------------------
Both repos run in the same process under one event loop (``main.run_agent``).
The executor is constructed with the :class:`~.telegram_gateway.TelegramGateway`
handle (5th positional arg of the ``ExecutorFactory``), so a Claude executor
already holds it — store it as ``self._gateway`` and pass it through:

    from superpos_agent_core import ask_user_question, PENDING_QUESTIONS

    answers = await ask_user_question(
        chat_id=req.chat_id,
        thread_id=req.thread_id,
        questions=questions,          # list[dict] mirroring AskUserQuestion
        gateway=self._gateway,
        timeout=600,
    )

The Telegram side resolves the future via the module-level
:data:`PENDING_QUESTIONS` registry, which the ``CallbackQueryHandler`` (wired
in :mod:`.telegram_bot`) and ``cancel_chat`` both reach.  No shared singleton
needs to be threaded through the executor — the registry is a process-global,
matching the single-event-loop architecture.

callback_data scheme
--------------------
``f"{ask_id}:{q_idx}:{opt_idx}"`` for an option tap, ``f"{ask_id}:{q_idx}:done"``
for a multi-select "Done" button.  Indices (never labels) keep us under
Telegram's 64-byte ``callback_data`` limit.  ``ask_id`` is a short random hex
token so stale taps from a previous question route nowhere.

One outstanding question per ``(chat_id, thread_id)``
----------------------------------------------------
A second concurrent ask for the same conversation is **refused** (raises
:class:`AskAlreadyPending`) rather than replacing the first — this keeps
``callback_data`` routing unambiguous and avoids silently abandoning a
question the user may still be answering.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .executor import chat_key

log = logging.getLogger(__name__)

# Sentinel returned (per question) when the user never answered.
NO_RESPONSE = "__no_response__"


# ── Question schema ───────────────────────────────────────────────────


@dataclass
class Option:
    """One selectable answer for a question (mirrors AskUserQuestion)."""

    label: str
    description: str = ""
    preview: str | None = None


@dataclass
class Question:
    """A single question with 2–4 options (mirrors AskUserQuestion)."""

    question: str
    header: str = ""
    options: list[Option] = field(default_factory=list)
    multi_select: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Question":
        """Build from the loose dict shape an MCP tool input uses.

        Accepts both ``multiSelect`` (SDK casing) and ``multi_select``.
        """
        opts_raw = raw.get("options") or []
        options = [
            Option(
                label=str(o["label"]),
                description=str(o.get("description", "") or ""),
                preview=o.get("preview"),
            )
            for o in opts_raw
        ]
        return cls(
            question=str(raw.get("question", "") or ""),
            header=str(raw.get("header", "") or ""),
            options=options,
            multi_select=bool(raw.get("multiSelect", raw.get("multi_select", False))),
        )


def _coerce_questions(questions: list[Any]) -> list[Question]:
    out: list[Question] = []
    for q in questions:
        out.append(q if isinstance(q, Question) else Question.from_dict(q))
    return out


# ── Pending-question registry ─────────────────────────────────────────


class AskAlreadyPending(RuntimeError):
    """Raised when a second question is asked for a conversation that already
    has one outstanding."""


@dataclass
class _PendingAsk:
    ask_id: str
    chat_id: int | str
    thread_id: int | None
    questions: list[Question]
    future: asyncio.Future
    # message_id per question index (filled as messages are sent).
    message_ids: dict[int, int] = field(default_factory=dict)
    # For multi-select: q_idx -> set of selected opt indices.
    selections: dict[int, set[int]] = field(default_factory=dict)
    # Set True when resolved by cancel_chat (/stop) rather than a user answer.
    cancelled: bool = False


class PendingQuestions:
    """Process-global map of outstanding interactive questions.

    Keyed by ``chat_key(chat_id, thread_id)`` so at most one question can be
    outstanding per conversation/topic.
    """

    def __init__(self) -> None:
        self._by_chat: dict[str, _PendingAsk] = {}
        self._by_ask_id: dict[str, _PendingAsk] = {}

    def register(
        self,
        chat_id: int | str,
        thread_id: int | None,
        questions: list[Question],
        future: asyncio.Future,
    ) -> _PendingAsk:
        key = chat_key(chat_id, thread_id)
        if key in self._by_chat:
            raise AskAlreadyPending(
                f"a question is already pending for chat {key}"
            )
        ask_id = secrets.token_hex(4)
        pending = _PendingAsk(
            ask_id=ask_id,
            chat_id=chat_id,
            thread_id=thread_id,
            questions=questions,
            future=future,
        )
        self._by_chat[key] = pending
        self._by_ask_id[ask_id] = pending
        return pending

    def get_by_ask_id(self, ask_id: str) -> _PendingAsk | None:
        return self._by_ask_id.get(ask_id)

    def get_by_chat(self, chat_id: int | str, thread_id: int | None) -> _PendingAsk | None:
        return self._by_chat.get(chat_key(chat_id, thread_id))

    def discard(self, pending: _PendingAsk) -> None:
        self._by_ask_id.pop(pending.ask_id, None)
        self._by_chat.pop(chat_key(pending.chat_id, pending.thread_id), None)

    def cancel_chat(self, chat_id_or_key: int | str, thread_id: int | None = None) -> int:
        """Cancel a pending question for a chat key (used by ``/stop``).

        ``chat_id_or_key`` may be a bare chat id (with optional ``thread_id``)
        or an already-composed ``chat_key`` string — both resolve here so the
        executor's ``cancel_chat`` (keyed by ``str(chat_id)`` or composite
        key) can call straight through.
        """
        if thread_id is not None:
            key = chat_key(chat_id_or_key, thread_id)
        else:
            key = str(chat_id_or_key)
        pending = self._by_chat.get(key)
        if pending is None:
            return 0
        self.discard(pending)
        if not pending.future.done():
            # Resolve (don't cancel) so the parked ask_user_question coroutine
            # unblocks and returns a no-response result.  A flag distinguishes
            # this from a genuine answer so the result reads as "timed out".
            pending.cancelled = True
            pending.future.set_result(False)
        return 1


# Module-level registry — the Telegram CallbackQueryHandler, ask_user_question,
# and cancel_chat all share this one instance (single event loop).
PENDING_QUESTIONS = PendingQuestions()


# ── Rendering ─────────────────────────────────────────────────────────


def _question_body(q: Question) -> str:
    lines: list[str] = []
    if q.header:
        lines.append(q.header)
    if q.question:
        lines.append(q.question)
    if lines:
        lines.append("")
    for i, opt in enumerate(q.options):
        line = f"{i + 1}. {opt.label}"
        if opt.description:
            line += f" — {opt.description}"
        lines.append(line)
        if opt.preview:
            lines.append(f"   {opt.preview}")
    if q.multi_select:
        lines.append("")
        lines.append("(Select one or more, then tap Done.)")
    return "\n".join(lines)


def _keyboard(
    pending: _PendingAsk, q_idx: int,
) -> InlineKeyboardMarkup:
    q = pending.questions[q_idx]
    selected = pending.selections.get(q_idx, set())
    rows: list[list[InlineKeyboardButton]] = []
    for opt_idx, opt in enumerate(q.options):
        label = opt.label
        if q.multi_select and opt_idx in selected:
            label = f"✓ {label}"
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"{pending.ask_id}:{q_idx}:{opt_idx}",
            )
        ])
    if q.multi_select:
        rows.append([
            InlineKeyboardButton(
                "✅ Done",
                callback_data=f"{pending.ask_id}:{q_idx}:done",
            )
        ])
    return InlineKeyboardMarkup(rows)


# ── Public coroutine ──────────────────────────────────────────────────


async def ask_user_question(
    chat_id: int | str,
    thread_id: int | None,
    questions: list[Any],
    *,
    gateway: Any,
    timeout: float = 600.0,
    registry: PendingQuestions | None = None,
) -> dict[str, Any]:
    """Ask the user one or more multiple-choice questions in Telegram.

    Parameters
    ----------
    chat_id, thread_id:
        The originating conversation; ``thread_id`` routes into a forum topic.
    questions:
        A list of question specs — either :class:`Question` instances or loose
        dicts shaped like AskUserQuestion's input::

            {"question": str, "header": str,
             "options": [{"label": str, "description": str, "preview": str?}],
             "multiSelect": bool}
    gateway:
        A :class:`~.telegram_gateway.TelegramGateway` (or anything exposing the
        same ``send_message`` / ``answer_callback_query`` / edit wrappers).
    timeout:
        Seconds to wait for the user before giving up.
    registry:
        Pending-question registry; defaults to the module-global
        :data:`PENDING_QUESTIONS`.

    Returns
    -------
    A JSON-serializable dict the MCP tool can return as the tool result::

        {
          "answers": [
            {"header": str, "question": str, "selected": [label, ...]},
            ...
          ],
          "timed_out": bool,
        }

    On timeout (or a cancelled chat) ``selected`` is ``[NO_RESPONSE]`` for any
    unanswered question and ``timed_out`` is ``True`` — the caller/model can
    then proceed without hanging.

    Raises
    ------
    AskAlreadyPending:
        If a question is already outstanding for this ``(chat_id, thread_id)``.
    """
    reg = registry or PENDING_QUESTIONS
    qs = _coerce_questions(questions)
    if not qs:
        return {"answers": [], "timed_out": False}

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    pending = reg.register(chat_id, thread_id, qs, future)

    # Single-question fast-path resolution is handled in the callback handler;
    # for multi-question asks we resolve only after every question is answered.
    # We render the first question now and subsequent ones as each resolves.
    try:
        await _send_question(gateway, pending, 0)

        try:
            await asyncio.wait_for(future, timeout=timeout)
            # cancel_chat resolves the future with False + sets `.cancelled`;
            # a real answer resolves with True.  Either way we fall through.
            timed_out = pending.cancelled
        except asyncio.TimeoutError:
            timed_out = True
        return _build_result(pending, timed_out)
    finally:
        reg.discard(pending)
        await _cleanup_messages(gateway, pending)


async def _send_question(gateway: Any, pending: _PendingAsk, q_idx: int) -> None:
    q = pending.questions[q_idx]
    msg = await gateway.send_message(
        pending.chat_id,
        _question_body(q),
        message_thread_id=pending.thread_id,
        reply_markup=_keyboard(pending, q_idx),
    )
    if msg is not None and getattr(msg, "message_id", None) is not None:
        pending.message_ids[q_idx] = msg.message_id


async def _cleanup_messages(gateway: Any, pending: _PendingAsk) -> None:
    """Strip inline keyboards off any sent question messages (best-effort)."""
    for q_idx, message_id in list(pending.message_ids.items()):
        try:
            q = pending.questions[q_idx]
            selected = _selected_labels(pending, q_idx)
            suffix = ", ".join(selected) if selected else "(no response)"
            await gateway.edit_message_text(
                pending.chat_id,
                message_id,
                f"{_question_body(q)}\n\n➡️ {suffix}",
            )
        except Exception:
            log.debug("Failed to clean up question message", exc_info=True)


def _selected_labels(pending: _PendingAsk, q_idx: int) -> list[str]:
    q = pending.questions[q_idx]
    idxs = sorted(pending.selections.get(q_idx, set()))
    return [q.options[i].label for i in idxs if 0 <= i < len(q.options)]


def _build_result(pending: _PendingAsk, timed_out: bool) -> dict[str, Any]:
    answers: list[dict[str, Any]] = []
    for q_idx, q in enumerate(pending.questions):
        selected = _selected_labels(pending, q_idx)
        if not selected and timed_out:
            selected = [NO_RESPONSE]
        answers.append({
            "header": q.header,
            "question": q.question,
            "selected": selected,
        })
    return {"answers": answers, "timed_out": timed_out}


# ── Callback resolution (called by the CallbackQueryHandler) ──────────


@dataclass
class CallbackResult:
    """Outcome of handling a callback_data tap, for the handler to act on."""

    handled: bool
    toast: str | None = None  # text for answer_callback_query
    rerender_q_idx: int | None = None  # multi-select toggle → re-render keyboard
    advance_to_q_idx: int | None = None  # send the next question
    resolved: bool = False  # the whole ask is now resolved


def parse_callback_data(data: str) -> tuple[str, int, int | None] | None:
    """Parse ``ask_id:q_idx:opt_idx|done`` → ``(ask_id, q_idx, opt_idx_or_None)``.

    Returns ``None`` for malformed data.  ``opt_idx`` is ``None`` for "done".
    """
    parts = data.split(":")
    if len(parts) != 3:
        return None
    ask_id, q_raw, opt_raw = parts
    try:
        q_idx = int(q_raw)
    except ValueError:
        return None
    if opt_raw == "done":
        return ask_id, q_idx, None
    try:
        return ask_id, q_idx, int(opt_raw)
    except ValueError:
        return None


def handle_callback(
    data: str,
    registry: PendingQuestions | None = None,
) -> CallbackResult:
    """Apply a callback tap to the registry; return what the handler should do.

    This is pure registry/state logic — the Telegram I/O (answering the
    callback query, editing the message, sending the next question) is left to
    the handler so this stays trivially testable.
    """
    reg = registry or PENDING_QUESTIONS
    parsed = parse_callback_data(data)
    if parsed is None:
        return CallbackResult(handled=False, toast="Invalid selection")
    ask_id, q_idx, opt_idx = parsed

    pending = reg.get_by_ask_id(ask_id)
    if pending is None:
        return CallbackResult(handled=True, toast="This question expired.")
    if q_idx < 0 or q_idx >= len(pending.questions):
        return CallbackResult(handled=True, toast="This question expired.")

    q = pending.questions[q_idx]

    if q.multi_select:
        if opt_idx is None:  # Done
            return _resolve_or_advance(reg, pending, q_idx)
        # Toggle.
        sel = pending.selections.setdefault(q_idx, set())
        if opt_idx in sel:
            sel.discard(opt_idx)
        else:
            sel.add(opt_idx)
        return CallbackResult(handled=True, rerender_q_idx=q_idx)

    # Single-select: record the one choice and advance/resolve.
    if opt_idx is None or opt_idx < 0 or opt_idx >= len(q.options):
        return CallbackResult(handled=True, toast="Invalid selection")
    pending.selections[q_idx] = {opt_idx}
    label = q.options[opt_idx].label
    result = _resolve_or_advance(reg, pending, q_idx)
    result.toast = f"Selected: {label}"
    return result


def _resolve_or_advance(
    reg: PendingQuestions, pending: _PendingAsk, q_idx: int,
) -> CallbackResult:
    """After a question is answered, either send the next one or resolve."""
    next_idx = q_idx + 1
    if next_idx < len(pending.questions):
        return CallbackResult(handled=True, advance_to_q_idx=next_idx)
    # Last question answered — resolve the future.
    if not pending.future.done():
        pending.future.set_result(True)
    return CallbackResult(handled=True, resolved=True)
