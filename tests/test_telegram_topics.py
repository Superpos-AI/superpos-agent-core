"""Tests for Telegram forum-topic support.

Covers the four layers a topic travels through:

* ``resolve_topic_thread`` — scoping incoming messages to the agent's
  bound topic and extracting the conversation thread id.
* ``chat_key`` / ``ExecutionRequest.chat_key`` — per-topic session keys
  that stay backward compatible for plain chats.
* ``BaseConfig.telegram_thread_id`` — env parsing of the topic binding.
* ``TelegramStreamer`` / ``TelegramGateway`` — outgoing sends landing in
  the topic the request came from.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatType

from superpos_agent_core.config import BaseConfig
from superpos_agent_core.executor import ExecutionRequest, chat_key
from superpos_agent_core.telegram_bot import resolve_topic_thread
from superpos_agent_core.telegram_gateway import TelegramGateway
from superpos_agent_core.telegram_streamer import TelegramStreamer


def _message(
    *,
    chat_type: str = ChatType.SUPERGROUP,
    thread_id: int | None = None,
    is_topic: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type),
        chat_id=-100123,
        message_thread_id=thread_id,
        is_topic_message=is_topic,
    )


# ── resolve_topic_thread ──────────────────────────────────────────────


def test_unbound_dm_has_no_thread():
    assert resolve_topic_thread(
        _message(chat_type=ChatType.PRIVATE), None,
    ) == (True, None)


def test_unbound_topic_message_extracts_thread():
    msg = _message(thread_id=42, is_topic=True)
    assert resolve_topic_thread(msg, None) == (True, 42)


def test_plain_group_reply_is_not_a_topic():
    # Replies in non-forum groups also carry message_thread_id, but
    # is_topic_message is False — they must NOT fork the session key.
    msg = _message(thread_id=555, is_topic=False)
    assert resolve_topic_thread(msg, None) == (True, None)


def test_bound_agent_handles_its_own_topic():
    msg = _message(thread_id=42, is_topic=True)
    assert resolve_topic_thread(msg, 42) == (True, 42)


def test_bound_agent_ignores_other_topics():
    msg = _message(thread_id=43, is_topic=True)
    assert resolve_topic_thread(msg, 42) == (False, 43)


def test_bound_agent_ignores_general_topic():
    # General (and plain group messages) carry no topic — out of scope
    # for a bound agent so it stays in its lane.
    msg = _message()
    assert resolve_topic_thread(msg, 42) == (False, None)


def test_bound_agent_still_answers_dms():
    msg = _message(chat_type=ChatType.PRIVATE)
    assert resolve_topic_thread(msg, 42) == (True, None)


# ── chat_key / ExecutionRequest ───────────────────────────────────────


def test_chat_key_plain_chat_is_backward_compatible():
    assert chat_key(123) == "123"
    assert chat_key("123") == "123"


def test_chat_key_with_thread_is_composite():
    assert chat_key(123, 42) == "123:42"


def test_execution_request_chat_key():
    req = ExecutionRequest(prompt="hi", chat_id=123, source="telegram")
    assert req.chat_key == "123"
    req = ExecutionRequest(
        prompt="hi", chat_id=123, source="telegram", thread_id=42,
    )
    assert req.chat_key == "123:42"


# ── config parsing ────────────────────────────────────────────────────


def test_telegram_thread_id_unset_is_none():
    assert BaseConfig().telegram_thread_id is None


def test_telegram_thread_id_parses_int():
    assert BaseConfig(telegram_topic_id="42").telegram_thread_id == 42


def test_telegram_thread_id_non_numeric_is_ignored():
    assert BaseConfig(telegram_topic_id="oops").telegram_thread_id is None


def test_topic_id_loaded_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOPIC_ID", "77")
    assert BaseConfig.from_env().telegram_topic_id == "77"


# ── streamer routes output into the topic ─────────────────────────────


async def test_streamer_sends_into_thread():
    gateway = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
    )
    s = TelegramStreamer(gateway, chat_id=123, thread_id=42)

    await s._send_formatted("hello")

    kwargs = gateway.send_message.await_args.kwargs
    assert kwargs["message_thread_id"] == 42


async def test_streamer_error_sends_into_thread():
    gateway = SimpleNamespace(send_message=AsyncMock(return_value=None))
    s = TelegramStreamer(gateway, chat_id=123, thread_id=42)

    await s.error("boom")

    kwargs = gateway.send_message.await_args.kwargs
    assert kwargs["message_thread_id"] == 42


async def test_streamer_without_thread_sends_top_level():
    gateway = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
    )
    s = TelegramStreamer(gateway, chat_id=123)

    await s._send_formatted("hello")

    kwargs = gateway.send_message.await_args.kwargs
    assert kwargs["message_thread_id"] is None


# ── gateway passes the thread to the Bot API (and strips None) ────────


async def _roundtrip_send(bot: AsyncMock, **send_kwargs) -> None:
    gw = TelegramGateway(bot, min_interval=0.0)
    run_task = asyncio.create_task(gw.run())
    try:
        await asyncio.wait_for(
            gw.send_message(123, "hi", **send_kwargs), timeout=5,
        )
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass


async def test_gateway_forwards_message_thread_id():
    bot = SimpleNamespace(send_message=AsyncMock(return_value="ok"))
    await _roundtrip_send(bot, message_thread_id=42)
    assert bot.send_message.await_args.kwargs["message_thread_id"] == 42


async def test_gateway_omits_none_thread_id():
    bot = SimpleNamespace(send_message=AsyncMock(return_value="ok"))
    await _roundtrip_send(bot)
    assert "message_thread_id" not in bot.send_message.await_args.kwargs
