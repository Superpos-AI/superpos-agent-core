"""Streams agent output to Telegram by editing messages in real-time.

All Telegram API calls are delegated to a :class:`TelegramGateway` instance
which serializes them through a single processing loop.  This class handles
only buffer management, markdown formatting, and message tracking.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest

from .redactor import redact
from .telegram_gateway import TelegramGateway

log = logging.getLogger(__name__)

MAX_MSG_LEN = 4000
MIN_EDIT_INTERVAL = 5.0  # seconds between edits (per-streamer)


def _is_not_modified(err: BadRequest) -> bool:
    return "message is not modified" in str(err).lower()

# -- Human-readable tool descriptions ----------------------------------------

_TOOL_LABELS: dict[str, str] = {
    "shell": "Running command",
    "file_read": "Reading",
    "file_write": "Writing",
    "file_edit": "Editing",
    "glob": "Searching files",
    "grep": "Searching code",
    "web_search": "Searching the web",
    "web_fetch": "Fetching page",
    "Bash": "Running command",
    "Read": "Reading",
    "Write": "Writing",
    "Edit": "Editing",
    "Glob": "Searching files",
    "Grep": "Searching code",
    "WebSearch": "Searching the web",
    "WebFetch": "Fetching page",
    "Agent": "Running sub-agent",
    "NotebookEdit": "Editing notebook",
    # Gemini CLI tool names
    "run_shell_command": "Running command",
    "read_file": "Reading",
    "write_file": "Writing",
    "edit_file": "Editing",
    "search_file_content": "Searching code",
    "google_web_search": "Searching the web",
    "web_fetch_url": "Fetching page",
}


def _humanize_tool(tool_name: str, tool_input: Any) -> str:
    """Create a human-readable one-liner for a tool invocation."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    label = _TOOL_LABELS.get(tool_name, f"Using {tool_name}")

    detail = ""
    if tool_name in ("shell", "Bash", "run_shell_command"):
        cmd = inp.get("command", inp.get("cmd", ""))
        detail = " ".join(cmd.split())
    elif tool_name in (
        "file_read", "file_write", "file_edit",
        "Read", "Write", "Edit",
        "read_file", "write_file", "edit_file",
    ):
        path = inp.get("file_path", inp.get("path", inp.get("absolute_path", "")))
        if path:
            detail = path.rsplit("/", 1)[-1]
    elif tool_name in ("glob", "Glob"):
        detail = inp.get("pattern", "")
    elif tool_name in ("grep", "Grep", "search_file_content"):
        detail = inp.get("pattern", "")
    elif tool_name in ("web_search", "WebSearch", "google_web_search"):
        detail = inp.get("query", "")
    elif tool_name in ("web_fetch", "WebFetch", "web_fetch_url"):
        detail = inp.get("url", inp.get("prompt", ""))
    elif tool_name in ("codex_agent", "Agent"):
        detail = inp.get("description", inp.get("prompt", ""))

    if detail:
        if len(detail) > 60:
            detail = detail[:57] + "..."
        return f"{label}: {detail}"
    return label


def md_to_telegram(text: str) -> str:
    """Convert GitHub-flavored Markdown to Telegram MarkdownV2."""
    code_blocks: list[str] = []

    def _save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", _save_code_block, text)

    inline_codes: list[str] = []

    def _save_inline(m: re.Match) -> str:
        inline_codes.append(m.group(0))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`]+`", _save_inline, text)

    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    parts = re.split(r"(\x00(?:CODEBLOCK|INLINE)\d+\x00)", text)
    result = []
    for part in parts:
        if part.startswith("\x00CODEBLOCK"):
            idx = int(part.strip("\x00").replace("CODEBLOCK", ""))
            result.append(code_blocks[idx])
        elif part.startswith("\x00INLINE"):
            idx = int(part.strip("\x00").replace("INLINE", ""))
            result.append(inline_codes[idx])
        else:
            part = re.sub(r"([_\[\]()~>+\-=|{}.!\\#])", r"\\\1", part)
            result.append(part)

    return "".join(result)


class TelegramStreamer:
    """Accumulates text and pushes it to Telegram via message editing.

    All Telegram I/O runs in a background flusher task so callers
    (the agent executor) never block on Telegram.  ``append`` and
    ``send_tool_notification`` only mutate local state and wake the
    flusher — if Telegram is rate-limited or unreachable, the agent keeps
    reading its stdout pipe and the flusher catches up later.
    """

    _FINISH_DRAIN_TIMEOUT = 30.0
    _ERROR_SEND_TIMEOUT = 5.0

    def __init__(
        self,
        gateway: TelegramGateway | None,
        chat_id: int | str,
        thread_id: int | None = None,
    ) -> None:
        self._gateway = gateway
        self._chat_id = chat_id
        # Forum topic (message_thread_id) all output lands in; None sends
        # to the chat top-level as before.
        self._thread_id = thread_id
        self._messages: list[int] = []
        self._buffer = ""
        self._last_edit: float = 0.0
        self._current_msg_id: int | None = None
        self._status_msg_id: int | None = None
        self._tool_count: int = 0
        self._status_description: str = ""
        self._status_started: float = 0.0
        self._status_ticker: asyncio.Task | None = None

        self._pending_text: str = ""
        self._pending_tool: tuple[str, Any] | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._flusher: asyncio.Task | None = None

    async def start(self) -> None:
        if not self._gateway:
            return
        self._current_msg_id = None
        self._buffer = ""
        self._last_edit = time.monotonic()
        asyncio.create_task(self._safe_chat_action())
        if self._flusher is None:
            self._flusher = asyncio.create_task(self._flush_loop())

    async def _safe_chat_action(self) -> None:
        try:
            await self._gateway.send_chat_action(
                self._chat_id, ChatAction.TYPING,
                message_thread_id=self._thread_id,
            )
        except Exception:
            pass

    async def append(self, text: str) -> None:
        """Enqueue text for the flusher — never blocks on Telegram."""
        if not text or not self._gateway:
            return
        self._pending_text += redact(text)
        self._wake.set()

    async def send_tool_notification(self, tool_name: str, tool_input: Any) -> None:
        """Enqueue a tool-activity notification for the flusher."""
        if not self._gateway:
            return
        self._pending_tool = (tool_name, tool_input)
        self._wake.set()

    async def finish(self) -> None:
        """Signal the flusher to drain remaining output and exit."""
        if not self._gateway:
            return
        self._closing = True
        self._wake.set()
        flusher = self._flusher
        if flusher is None:
            return
        try:
            await asyncio.wait_for(flusher, timeout=self._FINISH_DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            flusher.cancel()
            try:
                await flusher
            except (asyncio.CancelledError, Exception):
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Flusher raised during finish", exc_info=True)
        finally:
            self._flusher = None

    async def _flush_loop(self) -> None:
        """Drain pending text/tool updates to Telegram in the background."""
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()

                pending_tool = self._pending_tool
                self._pending_tool = None
                pending_text = self._pending_text
                self._pending_text = ""

                try:
                    if pending_tool is not None:
                        await self._handle_tool_notification(*pending_tool)
                    if pending_text:
                        self._buffer += pending_text
                        await self._render_buffer()
                except Exception:
                    log.warning("Flush iteration failed (non-fatal)", exc_info=True)

                if self._closing and not self._pending_text and self._pending_tool is None:
                    try:
                        await self._final_drain()
                    except Exception:
                        log.warning("Final drain failed", exc_info=True)
                    return

                await asyncio.sleep(MIN_EDIT_INTERVAL)
        except asyncio.CancelledError:
            raise

    async def _render_buffer(self) -> None:
        if self._current_msg_id is None:
            msg = await self._send_formatted(self._buffer[:4096])
            if msg is None:
                return
            self._current_msg_id = msg.message_id
            self._messages.append(msg.message_id)
            self._last_edit = time.monotonic()
            return

        if len(self._buffer) > MAX_MSG_LEN:
            await self._finalize_current()
            return

        now = time.monotonic()
        if now - self._last_edit >= MIN_EDIT_INTERVAL:
            await self._edit_current()

    async def _final_drain(self) -> None:
        await self._delete_status()

        if not self._buffer:
            return

        if self._current_msg_id is None:
            msg = await self._send_formatted(self._buffer[:4096])
            if msg is None:
                return
            self._current_msg_id = msg.message_id
            self._messages.append(msg.message_id)
            return

        if len(self._buffer) > MAX_MSG_LEN:
            await self._finalize_current()

        await self._edit_current()

    async def _handle_tool_notification(self, tool_name: str, tool_input: Any) -> None:
        if self._current_msg_id and self._buffer.strip():
            try:
                await self._edit_current()
            except Exception:
                pass
            self._current_msg_id = None
            self._buffer = ""

        self._tool_count += 1
        self._status_description = redact(_humanize_tool(tool_name, tool_input))
        self._status_started = time.monotonic()

        if self._status_ticker and not self._status_ticker.done():
            self._status_ticker.cancel()

        await self._update_status_text()

        self._status_ticker = asyncio.create_task(self._run_status_ticker())

    async def _run_status_ticker(self) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                await self._update_status_text()
        except asyncio.CancelledError:
            pass

    def _format_elapsed(self) -> str:
        elapsed = int(time.monotonic() - self._status_started)
        if elapsed < 60:
            return f"{elapsed}s"
        return f"{elapsed // 60}m {elapsed % 60:02d}s"

    async def _update_status_text(self) -> None:
        elapsed = self._format_elapsed()
        status_text = f"⏳ {self._status_description} ({elapsed})"
        try:
            if self._status_msg_id is None:
                msg = await self._gateway.send_message(
                    self._chat_id, status_text,
                    message_thread_id=self._thread_id,
                )
                if msg is not None:
                    self._status_msg_id = msg.message_id
            else:
                await self._gateway.edit_message_text(
                    self._chat_id, self._status_msg_id, status_text,
                )
        except BadRequest as e:
            # "not modified" is harmless. Anything else (message deleted,
            # too old, identifier invalid) means we can never edit this
            # message again — drop the id so the next tick sends a fresh
            # status message instead of silently failing forever.
            if not _is_not_modified(e):
                self._status_msg_id = None

    async def error(self, error_text: str) -> None:
        """Send an error message (fire-and-forget — must never crash or hang)."""
        if not self._gateway:
            return
        try:
            await asyncio.wait_for(
                self._gateway.send_message(
                    self._chat_id, f"❌ {redact(error_text)}",
                    message_thread_id=self._thread_id,
                ),
                timeout=self._ERROR_SEND_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("Timed out sending error message to Telegram")
        except Exception:
            log.warning("Failed to send error message to Telegram", exc_info=True)

    # -- Internal -------------------------------------------------------------

    async def _send_formatted(self, text: str) -> Any:
        try:
            return await self._gateway.send_message(
                self._chat_id,
                md_to_telegram(text),
                parse_mode=ParseMode.MARKDOWN_V2,
                message_thread_id=self._thread_id,
            )
        except BadRequest:
            try:
                return await self._gateway.send_message(
                    self._chat_id, text,
                    message_thread_id=self._thread_id,
                )
            except Exception:
                return None

    async def _delete_status(self) -> None:
        if self._status_ticker and not self._status_ticker.done():
            self._status_ticker.cancel()
            self._status_ticker = None
        if self._status_msg_id is not None:
            try:
                await self._gateway.delete_message(self._chat_id, self._status_msg_id)
            except Exception:
                pass
            self._status_msg_id = None

    async def _edit_current(self) -> None:
        if not self._current_msg_id or not self._buffer:
            return
        try:
            formatted = md_to_telegram(self._buffer[:4096])
            await self._gateway.edit_message_text(
                self._chat_id,
                self._current_msg_id,
                formatted,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            self._last_edit = time.monotonic()
        except BadRequest as e:
            if _is_not_modified(e):
                return
            log.warning("Markdown parse failed, falling back to plain text: %s", e)
            try:
                await self._gateway.edit_message_text(
                    self._chat_id,
                    self._current_msg_id,
                    self._buffer[:4096],
                )
                self._last_edit = time.monotonic()
            except BadRequest:
                # Plain-text retry also rejected — the message is gone,
                # too old to edit, or otherwise unreachable.  Drop the id
                # so the next render sends a new message; without this we
                # silently loop forever on the same dead id and the user
                # stops seeing any further streaming output.
                self._current_msg_id = None
            except Exception:
                pass

    async def _finalize_current(self) -> None:
        finalize_text = self._buffer[:MAX_MSG_LEN]
        overflow = self._buffer[MAX_MSG_LEN:]

        self._buffer = finalize_text
        await self._edit_current()

        msg = await self._send_formatted(overflow or "...")
        if msg is None:
            return
        self._current_msg_id = msg.message_id
        self._messages.append(msg.message_id)
        self._buffer = overflow or ""
        self._last_edit = time.monotonic()
