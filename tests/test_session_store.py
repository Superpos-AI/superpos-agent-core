"""Tests for the versioned SessionStore."""

from __future__ import annotations

import json

import pytest

from superpos_agent_core import SessionStore


# ── Basic round-trip ─────────────────────────────────────────────────────


def test_set_then_get(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set("chat1", "sess-a")
    assert store.get("chat1") == "sess-a"


def test_get_missing_returns_none(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    assert store.get("nope") is None


def test_clear_removes_entry(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set("chat1", "sess-a")
    store.clear("chat1")
    assert store.get("chat1") is None


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "sessions.json")
    store1 = SessionStore(path)
    store1.set("chat1", "sess-a")
    store2 = SessionStore(path)
    assert store2.get("chat1") == "sess-a"


def test_corrupt_json_starts_fresh(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{not valid json")
    store = SessionStore(str(path))
    assert store.get("chat1") is None


# ── Versioned API ────────────────────────────────────────────────────────


def test_set_with_version_then_get_with_version(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set_with_version("chat1", "session-abc", 3)
    assert store.get_with_version("chat1") == ("session-abc", 3)
    # plain get still works
    assert store.get("chat1") == "session-abc"


def test_get_with_version_missing_returns_none(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    assert store.get_with_version("nope") is None


def test_set_records_none_version(tmp_path):
    """Plain set() stores persona_version=None — the entry exists but
    is exempt from invalidate_older_than() because we have no basis
    for comparison."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set("chat1", "sess-a")
    assert store.get_with_version("chat1") == ("sess-a", None)


def test_version_persists_across_instances(tmp_path):
    path = str(tmp_path / "sessions.json")
    store1 = SessionStore(path)
    store1.set_with_version("chat1", "session-abc", 2)
    store2 = SessionStore(path)
    assert store2.get_with_version("chat1") == ("session-abc", 2)


# ── Backward compatibility ───────────────────────────────────────────────


def test_legacy_plain_string_loads_as_version_zero(tmp_path):
    """Pre-versioning session files stored plain session_id strings.

    They load as version=0 so the next persona update naturally
    invalidates them via invalidate_older_than() — preferable to
    treating them as "unknown" and exempting them forever.
    """
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"chat1": "legacy-session"}))
    store = SessionStore(str(path))
    assert store.get_with_version("chat1") == ("legacy-session", 0)
    assert store.get("chat1") == "legacy-session"


# ── invalidate_older_than ────────────────────────────────────────────────


def test_invalidate_older_than_drops_stale_only(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set_with_version("chat_old", "s1", 1)
    store.set_with_version("chat_current", "s2", 5)
    store.set_with_version("chat_future", "s3", 7)
    dropped = store.invalidate_older_than(5)
    assert dropped == 1
    assert store.get("chat_old") is None
    assert store.get("chat_current") == "s2"
    assert store.get("chat_future") == "s3"


def test_invalidate_preserves_version_none_entries(tmp_path):
    """Entries with no version (set via legacy ``set()``) are kept —
    we have no basis for comparison so we can't say they're stale."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set("chat_unknown", "s1")  # persona_version=None
    dropped = store.invalidate_older_than(10)
    assert dropped == 0
    assert store.get("chat_unknown") == "s1"


def test_invalidate_returns_zero_when_no_stale(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set_with_version("chat1", "s1", 5)
    assert store.invalidate_older_than(5) == 0
    assert store.get("chat1") == "s1"


def test_invalidate_persists_to_disk(tmp_path):
    """Dropped entries must survive a reload — otherwise the same stale
    sessions come back on restart."""
    path = str(tmp_path / "sessions.json")
    store1 = SessionStore(path)
    store1.set_with_version("chat_old", "s1", 1)
    store1.set_with_version("chat_new", "s2", 5)
    store1.invalidate_older_than(3)
    store2 = SessionStore(path)
    assert store2.get("chat_old") is None
    assert store2.get("chat_new") == "s2"


# ── active_session_ids ───────────────────────────────────────────────────


def test_active_session_ids_returns_all_values(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set("chat1", "sess-a")
    store.set_with_version("chat2", "sess-b", 3)
    assert store.active_session_ids() == {"sess-a", "sess-b"}


def test_active_session_ids_empty_when_no_entries(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    assert store.active_session_ids() == set()
