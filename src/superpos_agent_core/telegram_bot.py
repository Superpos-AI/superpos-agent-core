"""Telegram bot daemon — receives messages and enqueues them on the agent's executor."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import BaseConfig
from .executor import Executor, ExecutionRequest
from .runtime_config import RuntimeConfig

log = logging.getLogger(__name__)

# Matches "PR #123", "#123", "pr #123", "PR#123", etc.
_PR_REF_RE = re.compile(r"(?:PR\s*)?#(\d+)", re.IGNORECASE)


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


async def run_telegram_bot(
    app: Application,
    executor: Executor,
    config: BaseConfig,
    runtime: RuntimeConfig,
) -> None:
    """Start the bot using non-blocking polling (compatible with asyncio.gather)."""

    allowed = set(config.telegram_allowed_users)
    known_models = type(runtime).KNOWN_MODELS
    effort_levels = type(runtime).EFFORT_LEVELS

    def is_allowed(user_id: int) -> bool:
        return not allowed or user_id in allowed

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(
            f"Hi! Send me any message and I'll process it with {config.executor_kind}."
        )

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(f"Queue depth: {executor.pending}")

    async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        executor.clear_session(update.effective_chat.id)
        await update.message.reply_text(
            "Session cleared. Next message starts a fresh conversation."
        )

    async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        await update.message.reply_text("Restarting...")
        log.info("Restart requested by user %s — sending SIGTERM", update.effective_user.id)
        os.kill(os.getpid(), signal.SIGTERM)

    async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Cancel any in-flight work for this chat.

        Targets only the calling chat — other chats keep running.  This
        is the kill-switch for "the agent went off the rails on the
        thing it's doing right now"; use ``/restart`` for a full reboot
        or ``/new`` to start a fresh conversation without interrupting
        current work.

        Three response paths:

        1. ``cancel_chat`` returned > 0 → success.
        2. ``cancel_chat`` returned 0 but the executor is busy → the
           concrete executor hasn't wired ``_track_chat_task``, so we
           can see work is running but can't target it.  Be explicit
           about that rather than misleading the user with "nothing in
           flight" — point them at ``/restart`` for a hard stop.
        3. ``cancel_chat`` returned 0 and the executor is idle →
           genuinely nothing to stop.
        """
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        chat_id = update.effective_chat.id
        cancelled = executor.cancel_chat(chat_id)
        if cancelled:
            log.info(
                "Cancelled %d in-flight task(s) for chat %s via /stop",
                cancelled, chat_id,
            )
            plural = "tasks" if cancelled != 1 else "task"
            await update.message.reply_text(
                f"⏹ Stopped {cancelled} in-flight {plural}.  Next message "
                f"starts fresh execution.",
            )
        elif executor.is_busy:
            log.warning(
                "/stop called for chat %s while executor is busy but no "
                "tasks are tracked — concrete executor hasn't wired "
                "_track_chat_task; pointing user at /restart.",
                chat_id,
            )
            await update.message.reply_text(
                "⚠️ Agent is busy, but this executor doesn't support "
                "per-chat cancellation yet.  Use /restart for a full "
                "reboot (interrupts every chat).",
            )
        else:
            await update.message.reply_text(
                "ℹ️ Nothing in flight to stop for this chat.",
            )

    async def cmd_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
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
        if not update.effective_user or not is_allowed(update.effective_user.id):
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
        if not update.effective_user or not is_allowed(update.effective_user.id):
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
        if not update.effective_user or not is_allowed(update.effective_user.id):
            log.warning("Unauthorized user %s attempted access", update.effective_user)
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
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued telegram message from user %s (queue=%d, branch=%s)",
            update.effective_user.id,
            executor.pending,
            branch,
        )

    async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
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
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued photo from user %s (queue=%d)",
            update.effective_user.id, executor.pending,
        )

    async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
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
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued voice message from user %s (queue=%d, transcript=%s...)",
            update.effective_user.id, executor.pending, transcript[:50],
        )

    async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
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
