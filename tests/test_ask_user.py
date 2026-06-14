"""Tests for interactive agent questions over Telegram (ask_user_question).

Covers the rendered inline keyboard, callback routing, single/multi-select,
the one-outstanding-per-chat policy, timeout sentinel, stale callbacks, and
cancel_chat integration.  The gateway is mocked like the existing telegram
tests; no network or real bot is involved.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from superpos_agent_core.ask_user import (
    NO_RESPONSE,
    AskAlreadyPending,
    PendingQuestions,
    Question,
    ask_user_question,
    handle_callback,
    parse_callback_data,
)


def _gateway() -> SimpleNamespace:
    """A mock gateway that returns an incrementing message_id per send."""
    state = {"next_id": 100}

    async def _send(*a, **k):
        mid = state["next_id"]
        state["next_id"] += 1
        return SimpleNamespace(message_id=mid)

    return SimpleNamespace(
        send_message=AsyncMock(side_effect=_send),
        edit_message_text=AsyncMock(return_value=None),
        edit_message_reply_markup=AsyncMock(return_value=None),
        answer_callback_query=AsyncMock(return_value=None),
    )


def _single_q(label_a="A", label_b="B") -> list[dict]:
    return [{
        "question": "Pick one",
        "header": "Choice",
        "options": [
            {"label": label_a, "description": "first"},
            {"label": label_b, "description": "second", "preview": "prev"},
        ],
        "multiSelect": False,
    }]


def _multi_q() -> list[dict]:
    return [{
        "question": "Pick many",
        "header": "Choices",
        "options": [
            {"label": "X"},
            {"label": "Y"},
            {"label": "Z"},
        ],
        "multiSelect": True,
    }]


# ── callback_data parsing ─────────────────────────────────────────────


def test_parse_callback_data_option():
    assert parse_callback_data("abcd:0:1") == ("abcd", 0, 1)


def test_parse_callback_data_done():
    assert parse_callback_data("abcd:2:done") == ("abcd", 2, None)


def test_parse_callback_data_malformed():
    assert parse_callback_data("abcd:notanint:1") is None
    assert parse_callback_data("toofew") is None
    assert parse_callback_data("a:0:notint") is None


# ── single-select: send keyboard, park, resolve ───────────────────────


async def test_single_select_sends_keyboard_and_resolves():
    reg = PendingQuestions()
    gw = _gateway()

    task = asyncio.create_task(
        ask_user_question(
            chat_id=123, thread_id=42, questions=_single_q(),
            gateway=gw, timeout=5, registry=reg,
        )
    )
    # Let the coroutine send the question and park on the future.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Message sent with the right inline keyboard + thread routing.
    send_kwargs = gw.send_message.await_args.kwargs
    assert send_kwargs["message_thread_id"] == 42
    markup = send_kwargs["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["A", "B"]
    pending = reg.get_by_chat(123, 42)
    assert pending is not None
    ask_id = pending.ask_id
    assert [b.callback_data for b in buttons] == [
        f"{ask_id}:0:0", f"{ask_id}:0:1",
    ]
    assert not task.done()  # parked

    # Simulate a callback tap on option index 1 (label "B").
    result = handle_callback(f"{ask_id}:0:1", reg)
    assert result.resolved is True
    assert result.toast == "Selected: B"

    answers = await asyncio.wait_for(task, timeout=2)
    assert answers["timed_out"] is False
    assert answers["answers"][0]["selected"] == ["B"]


# ── multi-select: toggles accumulate, only Done resolves ──────────────


async def test_multi_select_accumulates_and_done_resolves():
    reg = PendingQuestions()
    gw = _gateway()

    task = asyncio.create_task(
        ask_user_question(
            chat_id=1, thread_id=None, questions=_multi_q(),
            gateway=gw, timeout=5, registry=reg,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    pending = reg.get_by_chat(1, None)
    ask_id = pending.ask_id

    # Toggle X (0) and Z (2) — neither resolves.
    r1 = handle_callback(f"{ask_id}:0:0", reg)
    assert r1.resolved is False
    assert r1.rerender_q_idx == 0
    r2 = handle_callback(f"{ask_id}:0:2", reg)
    assert r2.resolved is False
    assert not task.done()

    # Done resolves with both selected.
    rd = handle_callback(f"{ask_id}:0:done", reg)
    assert rd.resolved is True

    answers = await asyncio.wait_for(task, timeout=2)
    assert answers["timed_out"] is False
    assert sorted(answers["answers"][0]["selected"]) == ["X", "Z"]


async def test_multi_select_toggle_off():
    reg = PendingQuestions()
    gw = _gateway()
    task = asyncio.create_task(
        ask_user_question(
            chat_id=1, thread_id=None, questions=_multi_q(),
            gateway=gw, timeout=5, registry=reg,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    ask_id = reg.get_by_chat(1, None).ask_id

    handle_callback(f"{ask_id}:0:1", reg)  # select Y
    handle_callback(f"{ask_id}:0:1", reg)  # deselect Y
    handle_callback(f"{ask_id}:0:done", reg)

    answers = await asyncio.wait_for(task, timeout=2)
    assert answers["answers"][0]["selected"] == []


# ── one outstanding per chat ──────────────────────────────────────────


async def test_second_concurrent_ask_refused():
    reg = PendingQuestions()
    gw = _gateway()
    task = asyncio.create_task(
        ask_user_question(
            chat_id=5, thread_id=7, questions=_single_q(),
            gateway=gw, timeout=5, registry=reg,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    with pytest.raises(AskAlreadyPending):
        await ask_user_question(
            chat_id=5, thread_id=7, questions=_single_q(),
            gateway=gw, timeout=5, registry=reg,
        )

    # First one still resolvable and registry not corrupted.
    ask_id = reg.get_by_chat(5, 7).ask_id
    handle_callback(f"{ask_id}:0:0", reg)
    answers = await asyncio.wait_for(task, timeout=2)
    assert answers["answers"][0]["selected"] == ["A"]


async def test_different_chats_can_both_ask():
    reg = PendingQuestions()
    gw = _gateway()
    t1 = asyncio.create_task(
        ask_user_question(chat_id=1, thread_id=None, questions=_single_q(),
                          gateway=gw, timeout=5, registry=reg)
    )
    t2 = asyncio.create_task(
        ask_user_question(chat_id=2, thread_id=None, questions=_single_q(),
                          gateway=gw, timeout=5, registry=reg)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    handle_callback(f"{reg.get_by_chat(1, None).ask_id}:0:0", reg)
    handle_callback(f"{reg.get_by_chat(2, None).ask_id}:0:1", reg)
    a1 = await asyncio.wait_for(t1, timeout=2)
    a2 = await asyncio.wait_for(t2, timeout=2)
    assert a1["answers"][0]["selected"] == ["A"]
    assert a2["answers"][0]["selected"] == ["B"]


# ── timeout sentinel + cleanup ────────────────────────────────────────


async def test_timeout_returns_sentinel_and_cleans_up():
    reg = PendingQuestions()
    gw = _gateway()
    answers = await ask_user_question(
        chat_id=9, thread_id=None, questions=_single_q(),
        gateway=gw, timeout=0.05, registry=reg,
    )
    assert answers["timed_out"] is True
    assert answers["answers"][0]["selected"] == [NO_RESPONSE]
    # No leaked future / registry entry.
    assert reg.get_by_chat(9, None) is None
    # Cleanup edited the message.
    assert gw.edit_message_text.await_count >= 1


# ── stale / unknown ask_id handled gracefully ─────────────────────────


def test_unknown_ask_id_answered_expired():
    reg = PendingQuestions()
    result = handle_callback("deadbeef:0:0", reg)
    assert result.handled is True
    assert "expired" in result.toast.lower()


def test_malformed_callback_data_handled():
    reg = PendingQuestions()
    result = handle_callback("garbage", reg)
    assert result.handled is False
    assert result.toast is not None


# ── cancel_chat cancels a pending question ────────────────────────────


async def test_cancel_chat_resolves_pending():
    reg = PendingQuestions()
    gw = _gateway()
    task = asyncio.create_task(
        ask_user_question(chat_id=77, thread_id=3, questions=_single_q(),
                          gateway=gw, timeout=30, registry=reg)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert reg.get_by_chat(77, 3) is not None

    # /stop targets the composite chat key.
    cancelled = reg.cancel_chat(77, 3)
    assert cancelled == 1

    answers = await asyncio.wait_for(task, timeout=2)
    assert answers["timed_out"] is True
    assert answers["answers"][0]["selected"] == [NO_RESPONSE]
    assert reg.get_by_chat(77, 3) is None


async def test_cancel_chat_by_composite_key_string():
    # Executor.cancel_chat is called with a chat_key string in topics.
    reg = PendingQuestions()
    gw = _gateway()
    task = asyncio.create_task(
        ask_user_question(chat_id=88, thread_id=5, questions=_single_q(),
                          gateway=gw, timeout=30, registry=reg)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert reg.cancel_chat("88:5") == 1
    answers = await asyncio.wait_for(task, timeout=2)
    assert answers["timed_out"] is True


def test_cancel_chat_no_pending_returns_zero():
    reg = PendingQuestions()
    assert reg.cancel_chat(1, 2) == 0


# ── multi-question advance ────────────────────────────────────────────


async def test_multiple_questions_advance_in_sequence():
    reg = PendingQuestions()
    gw = _gateway()
    questions = [
        {"question": "Q1", "options": [{"label": "a1"}, {"label": "a2"}]},
        {"question": "Q2", "options": [{"label": "b1"}, {"label": "b2"}]},
    ]
    task = asyncio.create_task(
        ask_user_question(chat_id=1, thread_id=None, questions=questions,
                          gateway=gw, timeout=5, registry=reg)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    ask_id = reg.get_by_chat(1, None).ask_id

    # Answer Q1 → advance (not resolved yet).
    r1 = handle_callback(f"{ask_id}:0:0", reg)
    assert r1.resolved is False
    assert r1.advance_to_q_idx == 1
    assert not task.done()

    # Answer Q2 → resolved.
    r2 = handle_callback(f"{ask_id}:1:1", reg)
    assert r2.resolved is True

    answers = await asyncio.wait_for(task, timeout=2)
    assert [a["selected"] for a in answers["answers"]] == [["a1"], ["b2"]]


# ── Question.from_dict accepts both casings ───────────────────────────


# ── CallbackQueryHandler wiring through register_handlers ─────────────


async def test_callback_handler_routes_tap_through_gate():
    """The wired CallbackQueryHandler answers the query, resolves the future,
    and respects the allowlist/topic gate."""
    from telegram.constants import ChatType
    from telegram.ext import Application, CallbackQueryHandler

    from superpos_agent_core.config import BaseConfig
    from superpos_agent_core.runtime_config import RuntimeConfig
    from superpos_agent_core.telegram_bot import register_handlers

    reg = PendingQuestions()
    gw = _gateway()

    # Park a question.
    task = asyncio.create_task(
        ask_user_question(chat_id=-100, thread_id=None, questions=_single_q(),
                          gateway=gw, timeout=5, registry=reg)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    ask_id = reg.get_by_chat(-100, None).ask_id

    config = BaseConfig(telegram_bot_token="123:dummy")
    runtime = RuntimeConfig(model="m", effort="medium", path="/tmp/rc.json")
    app = Application.builder().token("123:dummy").build()
    register_handlers(app, _StubExecutor(), config, runtime,
                      gateway=gw, pending_questions=reg)

    callback_cb = None
    for group in app.handlers.values():
        for h in group:
            if isinstance(h, CallbackQueryHandler):
                callback_cb = h.callback
    assert callback_cb is not None, "CallbackQueryHandler must be registered"

    # Build a fake callback_query update (user taps option index 0 → "A").
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.PRIVATE, id=-100),
        chat_id=-100,
        message_thread_id=None,
        is_topic_message=False,
    )
    query = SimpleNamespace(id="cbq", data=f"{ask_id}:0:0")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100),
    )
    await callback_cb(update, None)

    gw.answer_callback_query.assert_awaited()
    answers = await asyncio.wait_for(task, timeout=2)
    assert answers["answers"][0]["selected"] == ["A"]


class _StubExecutor:
    def clear_session(self, key): ...
    def cancel_chat(self, key): return 0
    @property
    def is_busy(self): return False
    @property
    def pending(self): return 0


def test_question_from_dict_casing():
    q = Question.from_dict({"question": "q", "multiSelect": True,
                            "options": [{"label": "x"}]})
    assert q.multi_select is True
    q2 = Question.from_dict({"question": "q", "multi_select": True,
                             "options": [{"label": "x"}]})
    assert q2.multi_select is True
