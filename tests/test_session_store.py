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
    assert store.get_with_version("chat1") == ("session-abc", 3, None)
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
    assert store.get_with_version("chat1") == ("sess-a", None, None)


def test_version_persists_across_instances(tmp_path):
    path = str(tmp_path / "sessions.json")
    store1 = SessionStore(path)
    store1.set_with_version("chat1", "session-abc", 2)
    store2 = SessionStore(path)
    assert store2.get_with_version("chat1") == ("session-abc", 2, None)


# ── Branch field ─────────────────────────────────────────────────────────


def test_set_with_branch_round_trip(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set_with_version("chat1", "session-abc", 3, branch="feat/foo")
    assert store.get_with_version("chat1") == ("session-abc", 3, "feat/foo")


def test_branch_persists_across_instances(tmp_path):
    path = str(tmp_path / "sessions.json")
    store1 = SessionStore(path)
    store1.set_with_version("chat1", "session-abc", 2, branch="feat/bar")
    store2 = SessionStore(path)
    assert store2.get_with_version("chat1") == ("session-abc", 2, "feat/bar")


def test_branch_overwrite_replaces_previous(tmp_path):
    """Saving a new session id with a different branch overwrites the old
    branch — the user has explicitly switched context, so the resume
    should follow the new cwd."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set_with_version("chat1", "session-1", 1, branch="feat/old")
    store.set_with_version("chat1", "session-2", 1, branch="feat/new")
    assert store.get_with_version("chat1") == ("session-2", 1, "feat/new")


def test_branch_omitted_defaults_to_none(tmp_path):
    """Callers that don't pass branch keep the same behavior as before
    the field existed — branch is None and the executor falls back to
    the default cwd."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set_with_version("chat1", "session-abc", 1)
    assert store.get_with_version("chat1") == ("session-abc", 1, None)


# ── Backward compatibility ───────────────────────────────────────────────


def test_legacy_plain_string_loads_as_version_zero(tmp_path):
    """Pre-versioning session files stored plain session_id strings.

    They load as version=0 so the next persona update naturally
    invalidates them via invalidate_older_than() — preferable to
    treating them as "unknown" and exempting them forever.  Branch
    is None because the legacy entry has no cwd context.
    """
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"chat1": "legacy-session"}))
    store = SessionStore(str(path))
    assert store.get_with_version("chat1") == ("legacy-session", 0, None)
    assert store.get("chat1") == "legacy-session"


def test_legacy_dict_without_branch_field_loads_with_none_branch(tmp_path):
    """Versioned entries written before the branch field existed should
    load with branch=None — the executor will treat them as "no
    worktree" and fall back to the default cwd, matching the
    pre-branch behavior.
    """
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({
        "chat1": {"session_id": "sess-a", "persona_version": 4},
    }))
    store = SessionStore(str(path))
    assert store.get_with_version("chat1") == ("sess-a", 4, None)


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


def test_invalidate_preserves_branch_on_surviving_entries(tmp_path):
    """Surviving entries keep their branch — invalidation drops stale
    rows wholesale, it does not silently rewrite the rest."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set_with_version("chat_keep", "s2", 5, branch="feat/bar")
    store.set_with_version("chat_drop", "s1", 1, branch="feat/foo")
    store.invalidate_older_than(3)
    assert store.get_with_version("chat_keep") == ("s2", 5, "feat/bar")
    assert store.get_with_version("chat_drop") is None


# ── active_session_ids ───────────────────────────────────────────────────


def test_active_session_ids_returns_all_values(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    store.set("chat1", "sess-a")
    store.set_with_version("chat2", "sess-b", 3)
    assert store.active_session_ids() == {"sess-a", "sess-b"}


def test_active_session_ids_empty_when_no_entries(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    assert store.active_session_ids() == set()
