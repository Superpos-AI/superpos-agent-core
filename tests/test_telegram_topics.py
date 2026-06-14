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
from telegram.ext import Application, CommandHandler

from superpos_agent_core.config import BaseConfig
from superpos_agent_core.executor import ExecutionRequest, chat_key
from superpos_agent_core.runtime_config import RuntimeConfig
from superpos_agent_core.telegram_bot import register_handlers, resolve_topic_thread
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


def test_legacy_session_keys_defaults_false():
    assert BaseConfig().telegram_legacy_session_keys is False


def test_legacy_session_keys_loaded_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_LEGACY_SESSION_KEYS", "true")
    assert BaseConfig.from_env().telegram_legacy_session_keys is True


def test_legacy_session_keys_env_falsey_stays_false(monkeypatch):
    monkeypatch.setenv("TELEGRAM_LEGACY_SESSION_KEYS", "no")
    assert BaseConfig.from_env().telegram_legacy_session_keys is False


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


async def test_gateway_forwards_reply_markup():
    bot = SimpleNamespace(send_message=AsyncMock(return_value="ok"))
    sentinel = object()
    await _roundtrip_send(bot, reply_markup=sentinel)
    assert bot.send_message.await_args.kwargs["reply_markup"] is sentinel


async def test_gateway_omits_none_reply_markup():
    bot = SimpleNamespace(send_message=AsyncMock(return_value="ok"))
    await _roundtrip_send(bot)
    assert "reply_markup" not in bot.send_message.await_args.kwargs


async def test_gateway_answer_callback_query_forwards_and_strips_none():
    bot = SimpleNamespace(answer_callback_query=AsyncMock(return_value="ok"))
    gw = TelegramGateway(bot, min_interval=0.0)
    run_task = asyncio.create_task(gw.run())
    try:
        await asyncio.wait_for(
            gw.answer_callback_query("cbq1", text="Selected: A"), timeout=5,
        )
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
    kwargs = bot.answer_callback_query.await_args.kwargs
    assert kwargs["callback_query_id"] == "cbq1"
    assert kwargs["text"] == "Selected: A"
    # show_alert was None → stripped.
    assert "show_alert" not in kwargs


async def test_gateway_edit_message_reply_markup_forwards():
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(return_value="ok"))
    gw = TelegramGateway(bot, min_interval=0.0)
    run_task = asyncio.create_task(gw.run())
    sentinel = object()
    try:
        await asyncio.wait_for(
            gw.edit_message_reply_markup(123, 9, reply_markup=sentinel),
            timeout=5,
        )
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
    kwargs = bot.edit_message_reply_markup.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert kwargs["message_id"] == 9
    assert kwargs["reply_markup"] is sentinel


# ── /new and /stop key scoping (strict default + legacy opt-in) ───────
#
# By default, topic-scoped /new and /stop address ONLY the composite
# "chat:thread" key, so they never touch the bare str(chat_id) session —
# which, on a migrated executor, is the legitimate General/plain-chat
# conversation for the same chat.
#
# An executor that hasn't migrated to ``req.chat_key`` instead stores its
# session and tracks its in-flight task under ``str(chat_id)`` even inside a
# topic.  For those, the operator opts in with
# ``telegram_legacy_session_keys=True`` (env TELEGRAM_LEGACY_SESSION_KEYS),
# which makes the bot also address the bare chat_id key as a migration
# bridge.  These tests pin both modes.


class _RecordingExecutor:
    """Minimal executor stand-in recording which keys it was addressed by."""

    def __init__(self, *, cancel_keys: set[str] | None = None) -> None:
        self.cleared: list[str] = []
        self.cancelled: list[str] = []
        # Keys that have "in-flight work" — cancel_chat returns 1 for these.
        self._cancel_keys = cancel_keys or set()
        self._active = bool(self._cancel_keys)

    def clear_session(self, key: str) -> None:
        self.cleared.append(key)

    def cancel_chat(self, key: str) -> int:
        self.cancelled.append(key)
        return 1 if key in self._cancel_keys else 0

    @property
    def is_busy(self) -> bool:
        return self._active


def _build_handlers(
    executor, *, bound_thread: int | None = None, legacy_keys: bool = False
):
    """Register handlers on a real Application and return them by command."""
    config = BaseConfig(
        telegram_bot_token="123:dummy",
        telegram_topic_id=str(bound_thread) if bound_thread is not None else "",
        telegram_legacy_session_keys=legacy_keys,
    )
    runtime = RuntimeConfig(model="m", effort="medium", path="/tmp/rc.json")
    app = Application.builder().token("123:dummy").build()
    register_handlers(app, executor, config, runtime)
    handlers: dict[str, object] = {}
    for group in app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler):
                for cmd in h.commands:
                    handlers[cmd] = h.callback
    return handlers


def _command_update(*, chat_id: int = -100123, thread_id: int | None = None):
    """Fake Update for a command, optionally inside a forum topic."""
    reply = AsyncMock()
    is_topic = thread_id is not None
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.SUPERGROUP, id=chat_id),
        chat_id=chat_id,
        message_thread_id=thread_id,
        is_topic_message=is_topic,
        reply_text=reply,
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
    )


async def test_cmd_new_in_topic_clears_only_topic_key_by_default():
    # Default (migrated executor): /new in a topic is strictly topic-scoped.
    # The bare str(chat_id) key — the General/plain-chat session — is left
    # untouched so unrelated work there isn't cleared.
    ex = _RecordingExecutor()
    handlers = _build_handlers(ex)
    await handlers["new"](_command_update(thread_id=42), None)
    assert ex.cleared == ["-100123:42"]


async def test_cmd_new_in_topic_clears_legacy_key_when_enabled():
    # Opt-in migration bridge: also clear the bare chat_id key so an
    # un-migrated executor (keyed by chat_id) drops the session too.
    ex = _RecordingExecutor()
    handlers = _build_handlers(ex, legacy_keys=True)
    await handlers["new"](_command_update(thread_id=42), None)
    assert ex.cleared == ["-100123:42", "-100123"]


async def test_cmd_new_plain_chat_clears_single_key():
    ex = _RecordingExecutor()
    handlers = _build_handlers(ex)
    await handlers["new"](_command_update(thread_id=None), None)
    # Plain chats collapse to one key — no redundant double-clear.
    assert ex.cleared == ["-100123"]


async def test_cmd_new_plain_chat_single_key_even_with_legacy_enabled():
    # The legacy flag only affects topics; plain chats are identical either
    # way (composite key already IS the bare key).
    ex = _RecordingExecutor()
    handlers = _build_handlers(ex, legacy_keys=True)
    await handlers["new"](_command_update(thread_id=None), None)
    assert ex.cleared == ["-100123"]


async def test_cmd_stop_in_topic_only_consults_topic_key_by_default():
    # Default: /stop in a topic must NOT fall back to the bare chat_id key,
    # so it can never cancel the chat's separate General/plain-chat work.
    ex = _RecordingExecutor(cancel_keys={"-100123"})
    handlers = _build_handlers(ex)
    update = _command_update(thread_id=42)
    await handlers["stop"](update, None)
    # Only the topic key is consulted; the bare-key work is left running.
    assert ex.cancelled == ["-100123:42"]
    reply = update.effective_message.reply_text
    assert "Nothing to stop" in reply.await_args.args[0]


async def test_cmd_stop_in_topic_falls_back_to_legacy_key_when_enabled():
    # Opt-in: un-migrated executor tracks work under the bare chat_id.
    ex = _RecordingExecutor(cancel_keys={"-100123"})
    handlers = _build_handlers(ex, legacy_keys=True)
    update = _command_update(thread_id=42)
    await handlers["stop"](update, None)
    # Topic key tried first (miss), then legacy key (hit).
    assert ex.cancelled == ["-100123:42", "-100123"]
    reply = update.effective_message.reply_text
    assert "Stopped 1" in reply.await_args.args[0]


async def test_cmd_stop_in_topic_prefers_topic_key_for_migrated_executor():
    # Even with legacy mode on, a migrated executor's work is tracked under
    # the composite key.  The topic key cancels, so we must NOT also hit the
    # legacy key (no double-cancel).
    ex = _RecordingExecutor(cancel_keys={"-100123:42"})
    handlers = _build_handlers(ex, legacy_keys=True)
    await handlers["stop"](_command_update(thread_id=42), None)
    assert ex.cancelled == ["-100123:42"]


async def test_cmd_stop_nothing_running_tries_both_keys_then_reports():
    # Legacy mode: both keys consulted; nothing cancelled anywhere.
    ex = _RecordingExecutor(cancel_keys=set())
    handlers = _build_handlers(ex, legacy_keys=True)
    update = _command_update(thread_id=42)
    await handlers["stop"](update, None)
    assert ex.cancelled == ["-100123:42", "-100123"]
    reply = update.effective_message.reply_text
    assert "Nothing to stop" in reply.await_args.args[0]


async def test_cmd_stop_plain_chat_consults_single_key():
    ex = _RecordingExecutor(cancel_keys={"-100123"})
    handlers = _build_handlers(ex)
    await handlers["stop"](_command_update(thread_id=None), None)
    assert ex.cancelled == ["-100123"]
