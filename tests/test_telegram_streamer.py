"""Tests for TelegramStreamer's recovery from non-recoverable BadRequest.

When ``edit_message_text`` raises a 400 that isn't "not modified" and the
plain-text fallback also fails, the streamer must drop the tracked message
id so the next render starts a new message — otherwise a single stale id
silently swallows every subsequent update.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from superpos_agent_core.telegram_streamer import TelegramStreamer


def _streamer_with_gateway(gateway: Any) -> TelegramStreamer:
    s = TelegramStreamer(gateway, chat_id=123)
    s._current_msg_id = 999
    s._buffer = "hello world"
    return s


async def test_edit_current_clears_msg_id_when_message_gone():
    gateway = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=BadRequest("Message to edit not found"),
        ),
    )
    s = _streamer_with_gateway(gateway)

    await s._edit_current()

    assert s._current_msg_id is None, (
        "non-recoverable 400 must drop the msg id so the next render "
        "creates a fresh message"
    )
    assert gateway.edit_message_text.await_count == 2, (
        "expected the markdown attempt + the plain-text retry"
    )


async def test_edit_current_keeps_msg_id_on_not_modified():
    gateway = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=BadRequest("Message is not modified"),
        ),
    )
    s = _streamer_with_gateway(gateway)

    await s._edit_current()

    assert s._current_msg_id == 999, "not-modified is harmless — keep the id"
    assert gateway.edit_message_text.await_count == 1, (
        "not-modified must not trigger the plain-text retry"
    )


async def test_edit_current_keeps_msg_id_when_plain_text_recovers():
    gateway = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=[
                BadRequest("Can't parse entities"),  # markdown-mode failure
                SimpleNamespace(message_id=999),     # plain-text retry succeeds
            ],
        ),
    )
    s = _streamer_with_gateway(gateway)

    await s._edit_current()

    assert s._current_msg_id == 999, (
        "plain-text retry succeeded — the message is still alive, keep id"
    )


async def test_update_status_text_clears_status_id_on_bad_request():
    gateway = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=BadRequest("Message to edit not found"),
        ),
        send_message=AsyncMock(),
    )
    s = TelegramStreamer(gateway, chat_id=123)
    s._status_msg_id = 555
    s._status_started = 0.0
    s._status_description = "thinking"

    await s._update_status_text()

    assert s._status_msg_id is None, (
        "status edit failed permanently — drop id so next tick sends a "
        "new status message"
    )


async def test_update_status_text_keeps_status_id_on_not_modified():
    gateway = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=BadRequest("Message is not modified"),
        ),
        send_message=AsyncMock(),
    )
    s = TelegramStreamer(gateway, chat_id=123)
    s._status_msg_id = 555
    s._status_started = 0.0
    s._status_description = "thinking"

    await s._update_status_text()

    assert s._status_msg_id == 555
