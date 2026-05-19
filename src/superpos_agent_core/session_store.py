"""Persistent session store: maps chat_id → (session_id, persona_version).

Each agent stores its CLI-specific session/thread ID per chat so follow-up
messages can resume the existing conversation instead of starting fresh.
The persona version is captured alongside so a downstream executor can
drop the resume when its current persona is newer than the one the
session was started under — preventing the LLM from inheriting an old
identity from conversation history written under a previous persona.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class SessionStore:
    def __init__(self, path: str) -> None:
        """``path`` is the JSON file location, typically ``{home_dir}/session_store.json``."""
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Failed to load session store, starting fresh")
            return
        # Backward compat: legacy entries stored as plain session_id strings.
        # Treat their persona_version as 0 so the next persona update
        # naturally invalidates them via invalidate_older_than().
        for chat_id, value in raw.items():
            if isinstance(value, str):
                self._data[chat_id] = {"session_id": value, "persona_version": 0}
            elif isinstance(value, dict) and "session_id" in value:
                self._data[chat_id] = {
                    "session_id": value["session_id"],
                    "persona_version": value.get("persona_version"),
                }
        log.info("Loaded %d session(s) from %s", len(self._data), self._path)

    def _save(self) -> None:
        """Atomically persist the session map.

        Writes to a sibling tempfile and renames on success.  If the disk
        is full ``write_text`` fails on the temp file, so the real file
        keeps its previous contents instead of being truncated to 0 bytes
        mid-write (the failure mode that wiped sessions when the Docker
        VM filled up).
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            try:
                tmp_path.write_text(json.dumps(self._data))
                tmp_path.replace(self._path)
            except OSError:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise
        except OSError:
            log.warning("Failed to persist session store to %s", self._path)

    def get(self, chat_id: int | str) -> str | None:
        entry = self._data.get(str(chat_id))
        return entry["session_id"] if entry else None

    def get_with_version(
        self, chat_id: int | str,
    ) -> tuple[str, int | None] | None:
        """Return (session_id, persona_version) or None.

        ``persona_version`` is None when the session was saved before any
        persona version was known (e.g. via legacy ``set()``).
        """
        entry = self._data.get(str(chat_id))
        if not entry:
            return None
        return entry["session_id"], entry.get("persona_version")

    def set(self, chat_id: int | str, session_id: str) -> None:
        """Persist a session id without a persona version.

        Agents that track persona versions should prefer
        ``set_with_version()``.  Sessions saved via this path are exempt
        from ``invalidate_older_than()`` (None has no basis for
        comparison).
        """
        self._data[str(chat_id)] = {
            "session_id": session_id,
            "persona_version": None,
        }
        self._save()

    def set_with_version(
        self,
        chat_id: int | str,
        session_id: str,
        persona_version: int | None,
    ) -> None:
        """Persist a session id paired with the current persona version."""
        self._data[str(chat_id)] = {
            "session_id": session_id,
            "persona_version": persona_version,
        }
        self._save()

    def clear(self, chat_id: int | str) -> None:
        self._data.pop(str(chat_id), None)
        self._save()

    def invalidate_older_than(self, persona_version: int) -> int:
        """Drop sessions whose stored persona_version is older than the given one.

        Sessions with ``persona_version=None`` (set via legacy ``set()``)
        are preserved — there's no basis for comparison.

        Returns the number of sessions dropped.
        """
        to_drop = [
            chat_id
            for chat_id, entry in self._data.items()
            if entry.get("persona_version") is not None
            and entry["persona_version"] < persona_version
        ]
        for chat_id in to_drop:
            del self._data[chat_id]
        if to_drop:
            self._save()
        return len(to_drop)

    def active_session_ids(self) -> set[str]:
        """Session IDs currently mapped to a chat.

        Disk-cleanup tooling (e.g. ``Executor.cleanup_stale_sessions``)
        must preserve these on startup — without it, idle chats whose
        session dir is older than the cleanup cutoff get their resume
        target deleted, and the next user message silently starts a
        fresh LLM session.
        """
        return {
            sid
            for entry in self._data.values()
            if (sid := entry.get("session_id"))
        }
