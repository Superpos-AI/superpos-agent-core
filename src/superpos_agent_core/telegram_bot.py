"""Telegram bot daemon — receives messages and enqueues them on the agent's executor."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess

import httpx
from telegram import Message, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import BaseConfig
from .executor import Executor, ExecutionRequest, chat_key
from .runtime_config import RuntimeConfig

log = logging.getLogger(__name__)

# Matches "PR #123", "#123", "pr #123", "PR#123", etc.
_PR_REF_RE = re.compile(r"(?:PR\s*)?#(\d+)", re.IGNORECASE)


def resolve_topic_thread(
    message: Message, bound_thread_id: int | None,
) -> tuple[bool, int | None]:
    """Topic-scope an incoming message; return ``(in_scope, thread_id)``.

    ``thread_id`` is the forum topic the message was posted in, or None
    for DMs, plain groups, and a forum's General topic (replies in plain
    groups also carry ``message_thread_id``, so it only counts when
    ``is_topic_message`` is set).

    When the agent is bound to a topic (``bound_thread_id``), group
    messages outside that topic are out of scope — several agents can
    then share one forum group with a topic each.  DMs always stay in
    scope so the operator can reach a bound agent directly.
    """
    thread_id = message.message_thread_id if message.is_topic_message else None
    if bound_thread_id is None:
        return True, thread_id
    if message.chat.type == ChatType.PRIVATE:
        return True, None
    return thread_id == bound_thread_id, thread_id


async def _resolve_pr_branch(pr_number: int, repo_dir: str) -> str | None:
    """Resolve a PR number to its head branch via `gh pr view`."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "gh", "pr", "view", str(pr_number),
                "--json", "headRefName",
                "--jq", ".headRefName",
                "-R", ".",
            ],
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            branch = result.stdout.strip()
            log.info("Resolved PR #%d -> branch %r", pr_number, branch)
            return branch
        log.debug("gh pr view failed for #%d: %s", pr_number, result.stderr.strip())
    except Exception:
        log.debug("Failed to resolve PR #%d branch", pr_number, exc_info=True)
    return None


async def _transcribe_voice(ogg_path: str, api_key: str) -> str | None:
    """Transcribe a voice message using OpenAI Whisper API.

    Whisper is small/cheap and works without the agent's LLM provider —
    Claude/Gemini/Qwen agents can set ``VOICE_TRANSCRIBE_API_KEY`` independently
    of their primary LLM credentials.
    """
    if not api_key:
        log.warning("Voice message received but no voice-transcribe API key set — skipping")
        return None
    try:
        async with httpx.AsyncClient() as client:
            with open(ogg_path, "rb") as f:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": ("voice.ogg", f, "audio/ogg")},
                    data={"model": "whisper-1"},
                    timeout=30.0,
                )
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            if text:
                log.info("Voice transcribed: %s...", text[:80])
            return text or None
    except Exception:
        log.warning("Voice transcription failed", exc_info=True)
        return None


def build_telegram_app(config: BaseConfig) -> Application:
    """Build a python-telegram-bot Application (do NOT call run_polling)."""
    return Application.builder().token(config.telegram_bot_token).build()


def register_handlers(
    app: Application,
    executor: Executor,
    config: BaseConfig,
    runtime: RuntimeConfig,
) -> None:
    """Wire all command/message handlers onto ``app``.

    Split out from :func:`run_telegram_bot` so the handler logic (which
    lives in closures over ``executor``/``config``) can be exercised in
    tests without standing up polling or touching the network.
    """

    allowed = set(config.telegram_allowed_users)
    bound_thread = config.telegram_thread_id
    legacy_session_keys = config.telegram_legacy_session_keys
    known_models = type(runtime).KNOWN_MODELS
    effort_levels = type(runtime).EFFORT_LEVELS

    def is_allowed(user_id: int) -> bool:
        return not allowed or user_id in allowed

    def gate(update: Update) -> tuple[bool, int | None]:
        """Allowlist + topic-scope check; returns ``(handle, thread_id)``.

        Every handler runs through this so commands and messages agree on
        which topic a conversation belongs to.
        """
        if not update.effective_user or not is_allowed(update.effective_user.id):
            log.warning("Unauthorized user %s attempted access", update.effective_user)
            return False, None
        message = update.effective_message
        if message is None:
            return False, None
        in_scope, thread_id = resolve_topic_thread(message, bound_thread)
        if not in_scope:
            log.debug(
                "Ignoring message outside bound topic %s (chat=%s, thread=%s)",
                bound_thread, message.chat_id, thread_id,
            )
        return in_scope, thread_id

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not gate(update)[0]:
            return
        await update.message.reply_text(
            f"Hi! Send me any message and I'll process it with {config.executor_kind}."
        )

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not gate(update)[0]:
            return
        await update.message.reply_text(f"Queue depth: {executor.pending}")

    def _session_keys(chat_id: int, thread_id: int | None) -> list[str]:
        """Keys to address for clear/cancel, newest-contract first.

        Always returns the topic-scoped ``chat:thread`` key for the calling
        conversation.  An executor that has switched to ``req.chat_key``
        stores its session/task under that composite key, so addressing it
        keeps ``/new``/``/stop`` strictly topic-scoped.

        Only when ``telegram_legacy_session_keys`` is enabled do we *also*
        address the bare ``str(chat_id)`` key, as a migration bridge for an
        executor still keyed on ``req.chat_id`` (whose topic work lives
        under the bare chat_id).  This is opt-in because on a migrated
        executor the bare key is the legitimate General/plain-chat session
        for the same chat — addressing it from a topic command would clear
        or cancel unrelated work there.  For plain chats the composite key
        already *is* the bare key, so the list collapses to a single key
        regardless of this flag.
        """
        keys = [chat_key(chat_id, thread_id)]
        if legacy_session_keys:
            legacy = chat_key(chat_id)
            if legacy not in keys:
                keys.append(legacy)
        return keys

    async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        in_scope, thread_id = gate(update)
        if not in_scope:
            return
        # Clear the topic-scoped key (and, only when legacy mode is on, the
        # bare chat_id key as a migration bridge).  clear_session is a no-op
        # for an unknown key.
        for key in _session_keys(update.effective_chat.id, thread_id):
            executor.clear_session(key)
        await update.message.reply_text(
            "Session cleared. Next message starts a fresh conversation."
        )

    async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not gate(update)[0]:
            return
        await update.message.reply_text("Restarting...")
        log.info("Restart requested by user %s — sending SIGTERM", update.effective_user.id)
        os.kill(os.getpid(), signal.SIGTERM)

    async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Cancel any in-flight work for this chat (or forum topic).

        Targets only the calling conversation — other chats and topics
        keep running.  This
        is the kill-switch for "the agent went off the rails on the
        thing it's doing right now"; use ``/restart`` for a full reboot
        or ``/new`` to start a fresh conversation without interrupting
        current work.

        Two response paths:

        1. ``cancel_chat`` returned > 0 → success.
        2. ``cancel_chat`` returned 0 → nothing was cancelled for this
           chat.  This could mean genuinely no work, OR untracked work
           in this chat (executor hasn't wired ``_track_chat_task``).
           We can't tell from here, so don't claim either way — just
           offer ``/restart`` as the always-works escape hatch *when
           there's any global busyness*, so the hint is contextually
           relevant.  Crucially, we never tell chat B that *its* work
           is uncancellable when in reality chat A is the busy one —
           ``is_busy`` is global, so claiming "this chat's executor
           doesn't support cancellation" based on it is misleading.
        """
        in_scope, thread_id = gate(update)
        if not in_scope:
            return
        # CommandHandler reprocesses edited commands, and on edit updates
        # update.message is None while update.effective_message is the
        # edited message.  Use effective_message so editing a /stop
        # message doesn't crash the handler with AttributeError.
        reply_target = update.effective_message
        if reply_target is None:
            return
        # Try the topic-scoped key first; only when legacy mode is enabled
        # do we fall back to the bare str(chat_id) key so /stop still cancels
        # work on executors that haven't migrated to req.chat_key.  We stop
        # at the first key that cancels something so a migrated executor
        # isn't double-cancelled (plain chats collapse to one key anyway).
        cancelled = 0
        key = chat_key(update.effective_chat.id, thread_id)
        for candidate in _session_keys(update.effective_chat.id, thread_id):
            cancelled = executor.cancel_chat(candidate)
            if cancelled:
                key = candidate
                break
        if cancelled:
            log.info(
                "Cancelled %d in-flight task(s) for chat %s via /stop",
                cancelled, key,
            )
            plural = "tasks" if cancelled != 1 else "task"
            await reply_target.reply_text(
                f"⏹ Stopped {cancelled} in-flight {plural}.  Next message "
                f"starts fresh execution.",
            )
            return

        message = "ℹ️ Nothing to stop for this chat."
        if executor.is_busy:
            # Something is running somewhere — could be this chat with
            # untracked work, could be another chat entirely.  Offer
            # /restart as a contextually-relevant hard-stop without
            # making any claim about which chat the busy work belongs to.
            message += (
                "\n\nIf you believe this chat is the one running work "
                "and you need to interrupt regardless, /restart reboots "
                "the whole agent (heavier — affects every chat)."
            )
        await reply_target.reply_text(message)

    async def cmd_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not gate(update)[0]:
            return
        counts = await asyncio.to_thread(executor.cleanup_stale_sessions, 24)
        freed_mb = counts.get("bytes_freed", 0) / (1024 * 1024)
        await update.message.reply_text(
            f"Cleaned up:\n"
            f"  Sessions: {counts.get('projects', 0)}\n"
            f"  Env snapshots: {counts.get('session_env', 0)}\n"
            f"  Freed: {freed_mb:.1f}MB"
        )

    async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not gate(update)[0]:
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text(
                f"Current model: `{runtime.model}`\n\n"
                f"Usage: `/model <id>` or `/model list`",
                parse_mode="Markdown",
            )
            return
        if args[0] == "list":
            listing = "\n".join(f"- `{m}`" for m in known_models) or "(no known models registered)"
            await update.message.reply_text(
                f"Known models:\n{listing}\n\n"
                f"Any valid model id is accepted — known list is a hint.",
                parse_mode="Markdown",
            )
            return
        try:
            runtime.set_model(args[0])
        except ValueError as e:
            await update.message.reply_text(f"Error: {e}")
            return
        log.info("Model changed to %s by user %s", runtime.model, update.effective_user.id)
        await update.message.reply_text(
            f"Model set to `{runtime.model}` (takes effect on next task).",
            parse_mode="Markdown",
        )

    async def cmd_effort(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not gate(update)[0]:
            return
        args = ctx.args or []
        if not args:
            levels = ", ".join(effort_levels)
            await update.message.reply_text(
                f"Current effort: `{runtime.effort}`\n\n"
                f"Usage: `/effort <{levels}>`",
                parse_mode="Markdown",
            )
            return
        try:
            runtime.set_effort(args[0])
        except ValueError as e:
            await update.message.reply_text(f"Error: {e}")
            return
        log.info("Effort changed to %s by user %s", runtime.effort, update.effective_user.id)
        await update.message.reply_text(
            f"Effort set to `{runtime.effort}` (takes effect on next task).",
            parse_mode="Markdown",
        )

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        in_scope, thread_id = gate(update)
        if not in_scope:
            return
        if not update.message or not update.message.text:
            return

        text = update.message.text
        branch: str | None = None
        if text.startswith("--branch "):
            parts = text.split(" ", 2)
            if len(parts) >= 2:
                branch = parts[1]
                text = parts[2] if len(parts) == 3 else ""

        if not branch and config.executor_worktree_isolation:
            match = _PR_REF_RE.search(text)
            if match:
                pr_num = int(match.group(1))
                branch = await _resolve_pr_branch(pr_num, config.executor_working_dir)

        req = ExecutionRequest(
            prompt=text,
            chat_id=update.effective_chat.id,
            source="telegram",
            branch=branch,
            thread_id=thread_id,
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued telegram message from user %s (queue=%d, branch=%s, thread=%s)",
            update.effective_user.id,
            executor.pending,
            branch,
            thread_id,
        )

    async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        in_scope, thread_id = gate(update)
        if not in_scope:
            return
        if not update.message or not update.message.photo:
            return

        largest = update.message.photo[-1]
        tg_file = await largest.get_file()
        path = f"/tmp/tg_photo_{update.message.message_id}.jpg"
        await tg_file.download_to_drive(path)

        caption = update.message.caption or "Analyze this image."
        branch: str | None = None
        if caption.startswith("--branch "):
            parts = caption.split(" ", 2)
            if len(parts) >= 2:
                branch = parts[1]
                caption = parts[2] if len(parts) == 3 else "Analyze this image."

        req = ExecutionRequest(
            prompt=caption,
            chat_id=update.effective_chat.id,
            source="telegram",
            branch=branch,
            image_paths=[path],
            thread_id=thread_id,
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued photo from user %s (queue=%d)",
            update.effective_user.id, executor.pending,
        )

    async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        in_scope, thread_id = gate(update)
        if not in_scope:
            return
        if not update.message or not update.message.voice:
            return

        voice = update.message.voice
        tg_file = await voice.get_file()
        ogg_path = f"/tmp/tg_voice_{update.message.message_id}.ogg"
        await tg_file.download_to_drive(ogg_path)

        transcript = await _transcribe_voice(ogg_path, config.voice_transcribe_api_key)
        try:
            os.unlink(ogg_path)
        except OSError:
            pass

        if not transcript:
            return

        req = ExecutionRequest(
            prompt=transcript,
            chat_id=update.effective_chat.id,
            source="telegram",
            thread_id=thread_id,
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued voice message from user %s (queue=%d, transcript=%s...)",
            update.effective_user.id, executor.pending, transcript[:50],
        )

    async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        in_scope, thread_id = gate(update)
        if not in_scope:
            return
        if not update.message or not update.message.document:
            return

        doc = update.message.document
        tg_file = await doc.get_file()
        filename = doc.file_name or f"file_{update.message.message_id}"
        path = f"/tmp/tg_doc_{update.message.message_id}_{filename}"
        await tg_file.download_to_drive(path)

        caption = update.message.caption or f"I sent you a file: {filename}. Work with it as needed."
        branch: str | None = None
        if caption.startswith("--branch "):
            parts = caption.split(" ", 2)
            if len(parts) >= 2:
                branch = parts[1]
                caption = parts[2] if len(parts) == 3 else (
                    f"I sent you a file: {filename}. Work with it as needed."
                )

        prompt = (
            f"The user sent a file. It has been saved to: {path}\n"
            f"File name: {filename}\n\n{caption}"
        )

        req = ExecutionRequest(
            prompt=prompt,
            chat_id=update.effective_chat.id,
            source="telegram",
            branch=branch,
            thread_id=thread_id,
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued document '%s' from user %s (queue=%d)",
            filename, update.effective_user.id, executor.pending,
        )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))


async def run_telegram_bot(
    app: Application,
    executor: Executor,
    config: BaseConfig,
    runtime: RuntimeConfig,
) -> None:
    """Start the bot using non-blocking polling (compatible with asyncio.gather)."""

    register_handlers(app, executor, config, runtime)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    log.info("Telegram bot started polling")

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        log.info("Telegram bot shutting down")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
