"""The ``/effort`` help must advertise the valid set for the *current* model.

Regression for the mismatch where ``cmd_effort`` rendered the static
``EFFORT_LEVELS`` union: a model-specific runtime (e.g. Codex) validates
``set_effort`` per model, so a GPT-5.6 agent was told ``minimal`` was valid
while a legacy agent was told ``none``/``xhigh``/``max`` were — values
``set_effort()`` then rejected. The usage string now reads through
``runtime.efforts_for_model(runtime.model)`` so the advertised contract
matches validation and tracks a mid-session ``/model`` switch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler

from superpos_agent_core.config import BaseConfig
from superpos_agent_core.runtime_config import RuntimeConfig
from superpos_agent_core.telegram_bot import register_handlers


class _PerModelRuntime(RuntimeConfig):
    """A runtime whose effort ladder narrows per model, like Codex."""

    _GPT_5_6 = ("none", "low", "medium", "high", "xhigh", "max")
    _LEGACY = ("minimal", "low", "medium", "high")
    EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

    @classmethod
    def efforts_for_model(cls, model: str) -> tuple[str, ...]:
        if model.startswith("gpt-5.6"):
            return cls._GPT_5_6
        return cls._LEGACY


class _StubExecutor:
    def clear_session(self, key): ...
    def cancel_chat(self, key): return 0

    @property
    def is_busy(self): return False

    @property
    def pending(self): return 0


def _effort_callback(runtime: RuntimeConfig):
    """Register handlers and pull out the wired ``/effort`` callback."""
    config = BaseConfig(telegram_bot_token="123:dummy")
    app = Application.builder().token("123:dummy").build()
    register_handlers(app, _StubExecutor(), config, runtime)
    for group in app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler) and "effort" in h.commands:
                return h.callback
    raise AssertionError("/effort handler must be registered")


def _no_arg_update() -> SimpleNamespace:
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.PRIVATE, id=-100),
        chat_id=-100,
        message_thread_id=None,
        is_topic_message=False,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100),
        message=message,
    )


async def _usage_text(runtime: RuntimeConfig) -> str:
    cb = _effort_callback(runtime)
    update = _no_arg_update()
    await cb(update, SimpleNamespace(args=[]))
    update.message.reply_text.assert_awaited_once()
    return update.message.reply_text.await_args.args[0]


async def test_effort_help_advertises_gpt_5_6_ladder():
    runtime = _PerModelRuntime(model="gpt-5.6-terra", effort="high", path="/tmp/rc.json")
    text = await _usage_text(runtime)
    assert "Usage: `/effort <none, low, medium, high, xhigh, max>`" in text
    # Legacy-only "minimal" must NOT be advertised for a GPT-5.6 model.
    assert "minimal" not in text


async def test_effort_help_advertises_legacy_ladder():
    runtime = _PerModelRuntime(model="gpt-5.5", effort="high", path="/tmp/rc.json")
    text = await _usage_text(runtime)
    assert "Usage: `/effort <minimal, low, medium, high>`" in text
    # GPT-5.6-only tiers must NOT be advertised for a legacy model.
    for tier in ("none", "xhigh", "max"):
        assert tier not in text


async def test_effort_help_tracks_mid_session_model_switch():
    """The help is evaluated per call, so a /model switch is reflected."""
    runtime = _PerModelRuntime(model="gpt-5.6-terra", effort="high", path="/tmp/rc.json")
    cb = _effort_callback(runtime)

    first = _no_arg_update()
    await cb(first, SimpleNamespace(args=[]))
    assert "xhigh" in first.message.reply_text.await_args.args[0]

    runtime.model = "gpt-5.5"  # simulate a /model switch
    second = _no_arg_update()
    await cb(second, SimpleNamespace(args=[]))
    text = second.message.reply_text.await_args.args[0]
    assert "minimal" in text
    assert "xhigh" not in text


def test_base_runtime_efforts_for_model_defaults_to_full_ladder():
    """Non-model-specific runtimes keep the global ladder (no behavior change)."""
    assert RuntimeConfig.efforts_for_model("anything") == RuntimeConfig.EFFORT_LEVELS
