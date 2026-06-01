"""Tests for registry_sync — Phase 2 of the Registry subsystem.

Real filesystem in tmpdirs, mock the HTTP client.  Mirrors the style
of ``test_sub_agent_sync.py``: each behaviour gets its own class, no
session-scoped fixtures, no monkeypatching beyond what the feature
under test actually needs.

Covers the quality-bar items from the PR brief:

- install diff (new desired items land on disk)
- uninstall diff (managed items absent from desired are removed)
- revision-drift reinstall (same slug, new revision marker)
- task-scope sandbox materialization + teardown
- ordered overlay lookup (task overlay wins, falls through to shared)
- feature flag off → no filesystem mutation
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from superpos_agent_core import registry_sync as rs
from superpos_agent_core.registry_sync import (
    MANAGED_MARKER_FILENAME,
    AgentScopeSyncResult,
    RegistryFetchError,
    RegistrySyncConfig,
    ResolvedItem,
    TaskScopeSyncResult,
    UnsafePathSegmentError,
    feature_enabled,
    materialise_items,
    resolve_path,
    sync_agent_scope,
    sync_task_scope,
)


# ── Fakes ─────────────────────────────────────────────────────────────


class FakeResolver:
    """In-memory replacement for :class:`RegistryResolverClient`.

    Tests pre-load the agent-scope and per-task responses; the call
    records every invocation so we can assert on it.
    """

    def __init__(
        self,
        agent_scope: dict[str, Any] | None = None,
        task_scopes: dict[str, dict[str, Any]] | None = None,
        raise_on: set[str] | None = None,
    ) -> None:
        self._agent_scope = agent_scope or {"items": []}
        self._task_scopes = task_scopes or {}
        self._raise_on = raise_on or set()
        self.calls: list[tuple[str, str | None]] = []

    def fetch_resolved(
        self, agent_id: str, task_id: str | None = None,
    ) -> dict[str, Any]:
        key = task_id or "<agent>"
        self.calls.append((agent_id, task_id))
        if key in self._raise_on:
            raise RegistryFetchError(f"fake failure for {key}")
        if task_id is None:
            return self._agent_scope
        return self._task_scopes.get(task_id, {"items": []})


# ── Helpers ───────────────────────────────────────────────────────────


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rs.FEATURE_FLAG_ENV, "1")


def _disable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rs.FEATURE_FLAG_ENV, raising=False)


def _subagent_payload(body: str = "Hello") -> dict[str, Any]:
    return {
        "frontmatter": {
            "description": "test sub",
            "model": "claude-opus-4-6",
            "tools": ["Read", "Edit"],
        },
        "body": body,
    }


def _skill_payload(
    instructions: str = "Skill instructions.",
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {"instructions": instructions, "files": files or []}


def _resolved_envelope(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "agent_context": {"agent_memory": None, "persona_version": None}}


def _make_resolved_item_dict(
    *,
    kind: str,
    slug: str,
    scope: str = "agent",
    revision_id: str = "rev-1",
    attachment_id: str = "att-1",
    payload: dict[str, Any] | None = None,
    deleted_at: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    if payload is None:
        payload = _subagent_payload() if kind == "subagent" else _skill_payload()
    return {
        "kind": kind,
        "slug": slug,
        "name": name or slug,
        "revision_id": revision_id,
        "payload": payload,
        "resolved_from_scope": scope,
        "resolved_from_attachment_id": attachment_id,
        "deleted_at": deleted_at,
    }


def _config(tmp_path: Path, sandbox: Path | None = None) -> RegistrySyncConfig:
    return RegistrySyncConfig(
        base_url="http://test.invalid",
        token="t",
        agent_id="agent-A",
        shared_roots={
            "subagent": str(tmp_path / "subagents"),
            "skill": str(tmp_path / "skills"),
            "module": str(tmp_path / "modules"),
        },
        sandbox_root=str(sandbox if sandbox is not None else tmp_path / "sandbox"),
    )


# ── feature_enabled() ────────────────────────────────────────────────


class TestFeatureEnabled:
    def test_default_off(self):
        # Pass explicit empty env so the host env can't accidentally
        # pre-enable the flag during the test run.
        assert feature_enabled({}) is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  on  "])
    def test_truthy(self, value):
        assert feature_enabled({rs.FEATURE_FLAG_ENV: value}) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
    def test_falsy(self, value):
        assert feature_enabled({rs.FEATURE_FLAG_ENV: value}) is False


# ── ResolvedItem.from_api ────────────────────────────────────────────


class TestResolvedItem:
    def test_round_trip_minimal(self):
        raw = _make_resolved_item_dict(kind="subagent", slug="coder")
        item = ResolvedItem.from_api(raw)
        assert item.kind == "subagent"
        assert item.slug == "coder"
        assert item.resolved_from_scope == "agent"
        assert item.revision_marker == "rev-1|att-1"

    def test_latest_marker_when_unpinned(self):
        raw = _make_resolved_item_dict(kind="skill", slug="lint", revision_id=None)
        # Server may emit ``revision_id: null`` for unpinned attachments.
        raw["revision_id"] = None
        item = ResolvedItem.from_api(raw)
        assert item.revision_marker == "latest|att-1"


# ── resolve_path() — ordered overlay lookup ──────────────────────────


class TestResolvePathOverlay:
    def test_shared_root_only(self, tmp_path: Path):
        shared = tmp_path / "shared"
        (shared / "coder").mkdir(parents=True)
        (shared / "coder" / "SKILL.md").write_text("x")
        found = resolve_path("skill", "coder", shared_root=str(shared))
        assert found is not None
        assert found == (shared / "coder").resolve()

    def test_task_overlay_wins(self, tmp_path: Path):
        shared = tmp_path / "shared"
        (shared / "coder").mkdir(parents=True)
        (shared / "coder" / "SKILL.md").write_text("shared")
        sandbox = tmp_path / "sb"
        (sandbox / "task-X" / "skill" / "coder").mkdir(parents=True)
        (sandbox / "task-X" / "skill" / "coder" / "SKILL.md").write_text("task")
        found = resolve_path(
            "skill", "coder",
            shared_root=str(shared),
            task_id="task-X",
            sandbox_root=str(sandbox),
        )
        assert found == (sandbox / "task-X" / "skill" / "coder").resolve()

    def test_falls_through_to_shared_when_not_overridden(self, tmp_path: Path):
        # A task that overrides one skill should still see other shared
        # skills that aren't overridden.  This is the "non-overridden
        # items are still visible inside a task" guarantee from §8.
        shared = tmp_path / "shared"
        (shared / "lint").mkdir(parents=True)
        (shared / "lint" / "SKILL.md").write_text("shared-lint")
        sandbox = tmp_path / "sb"
        (sandbox / "task-X" / "skill" / "coder").mkdir(parents=True)
        (sandbox / "task-X" / "skill" / "coder" / "SKILL.md").write_text("task-coder")

        # `lint` isn't in the task overlay → falls through to shared.
        found_lint = resolve_path(
            "skill", "lint",
            shared_root=str(shared),
            task_id="task-X",
            sandbox_root=str(sandbox),
        )
        assert found_lint == (shared / "lint").resolve()

        # `coder` is in the task overlay → wins.
        found_coder = resolve_path(
            "skill", "coder",
            shared_root=str(shared),
            task_id="task-X",
            sandbox_root=str(sandbox),
        )
        assert found_coder == (sandbox / "task-X" / "skill" / "coder").resolve()

    def test_md_suffix_accepted(self, tmp_path: Path):
        # Subagents land as ``<slug>.md`` files in the shared root
        # under the legacy layout — the overlay helper should still
        # find them.
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "coder.md").write_text("---\nname: coder\n---\n")
        found = resolve_path("subagent", "coder", shared_root=str(shared))
        assert found == (shared / "coder.md").resolve()

    def test_returns_none_when_missing(self, tmp_path: Path):
        shared = tmp_path / "shared"
        shared.mkdir()
        assert resolve_path("skill", "nope", shared_root=str(shared)) is None


# ── sync_agent_scope() — Phase 1 startup sync ────────────────────────


class TestSyncAgentScope:
    def test_feature_flag_off_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _disable_flag(monkeypatch)
        config = _config(tmp_path)
        # Pre-populate a shared root with an unrelated file; if the
        # sync were to run, it'd at least mkdir.  With the flag off we
        # require zero filesystem mutation.
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(kind="subagent", slug="coder"),
            ]),
        )
        results = sync_agent_scope(config, client=resolver)
        for kind in config.shared_roots:
            assert results[kind].skipped is True
            assert results[kind].installed == []
        # No HTTP call was made — the resolver client should be
        # untouched in the flag-off path.
        assert resolver.calls == []
        # And no directory was created.
        assert not Path(config.shared_roots["subagent"]).exists()

    def test_installs_new_desired_items(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(kind="subagent", slug="coder"),
                _make_resolved_item_dict(
                    kind="skill", slug="lint",
                    payload=_skill_payload("Lint instructions.", [
                        {"path": "scripts/lint.sh", "content": "#!/bin/bash\necho hi\n", "mode": "+x"},
                    ]),
                ),
            ]),
        )
        results = sync_agent_scope(config, client=resolver)
        assert results["subagent"].installed == ["coder"]
        assert results["skill"].installed == ["lint"]
        # Subagent landed with the right shape (frontmatter + body).
        sub_md = Path(config.shared_roots["subagent"]) / "coder" / "coder.md"
        assert sub_md.exists()
        text = sub_md.read_text()
        assert "name: coder" in text
        assert "model: claude-opus-4-6" in text
        # Skill landed with SKILL.md + executable helper script.
        skill_md = Path(config.shared_roots["skill"]) / "lint" / "SKILL.md"
        assert skill_md.read_text() == "Lint instructions."
        helper = Path(config.shared_roots["skill"]) / "lint" / "scripts" / "lint.sh"
        assert helper.exists()
        assert os.access(helper, os.X_OK)
        # Marker recorded so the next sync knows it's managed.
        marker = json.loads(
            (Path(config.shared_roots["skill"]) / "lint" / MANAGED_MARKER_FILENAME).read_text(),
        )
        assert marker["slug"] == "lint"
        assert marker["revision_marker"].startswith("rev-1|")

    def test_uninstall_diff_removes_managed_items_absent_from_desired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        # First pass installs both items.
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(kind="skill", slug="lint"),
                _make_resolved_item_dict(
                    kind="skill", slug="format", attachment_id="att-2",
                ),
            ]),
        )
        sync_agent_scope(config, client=resolver)
        assert (Path(config.shared_roots["skill"]) / "lint").is_dir()
        assert (Path(config.shared_roots["skill"]) / "format").is_dir()

        # Second pass: ``format`` no longer in desired set.
        resolver2 = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(kind="skill", slug="lint"),
            ]),
        )
        results = sync_agent_scope(config, client=resolver2)
        assert results["skill"].uninstalled == ["format"]
        assert not (Path(config.shared_roots["skill"]) / "format").exists()
        # ``lint`` was untouched.
        assert (Path(config.shared_roots["skill"]) / "lint" / "SKILL.md").exists()

    def test_revision_drift_triggers_reinstall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        # First pass: revision rev-1.
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(
                    kind="skill", slug="lint",
                    revision_id="rev-1",
                    payload=_skill_payload("old instructions"),
                ),
            ]),
        )
        sync_agent_scope(config, client=resolver)
        skill_md = Path(config.shared_roots["skill"]) / "lint" / "SKILL.md"
        assert skill_md.read_text() == "old instructions"

        # Second pass: server bumped to rev-2.
        resolver2 = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(
                    kind="skill", slug="lint",
                    revision_id="rev-2",
                    payload=_skill_payload("new instructions"),
                ),
            ]),
        )
        results = sync_agent_scope(config, client=resolver2)
        assert results["skill"].reinstalled == ["lint"]
        assert results["skill"].installed == []
        assert results["skill"].uninstalled == []
        assert skill_md.read_text() == "new instructions"

    def test_idempotent_on_no_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(kind="skill", slug="lint"),
            ]),
        )
        sync_agent_scope(config, client=resolver)
        results = sync_agent_scope(config, client=resolver)
        # Second pass is a no-op — neither install nor reinstall.
        assert results["skill"].installed == []
        assert results["skill"].reinstalled == []
        assert results["skill"].uninstalled == []

    def test_skips_task_scope_items(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Phase 1 is "hive + agent only".  If the server somehow
        # includes a task-scoped item in an agent-scope response, we
        # must ignore it here — it belongs in the per-task sandbox.
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(kind="skill", slug="lint", scope="agent"),
                _make_resolved_item_dict(kind="skill", slug="taskonly", scope="task"),
            ]),
        )
        results = sync_agent_scope(config, client=resolver)
        assert results["skill"].installed == ["lint"]
        assert not (Path(config.shared_roots["skill"]) / "taskonly").exists()

    def test_leaves_unmanaged_legacy_dirs_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        # Hand-roll an unmanaged skill dir.
        legacy = Path(config.shared_roots["skill"]) / "human-written"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("don't delete me")
        resolver = FakeResolver(agent_scope=_resolved_envelope([]))
        results = sync_agent_scope(config, client=resolver)
        assert results["skill"].uninstalled == []
        assert (legacy / "SKILL.md").read_text() == "don't delete me"

    def test_fetch_failure_leaves_existing_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        # Pre-populate via a successful sync.
        good = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(kind="skill", slug="lint"),
            ]),
        )
        sync_agent_scope(config, client=good)
        # Now simulate a fetch failure.
        bad = FakeResolver(raise_on={"<agent>"})
        results = sync_agent_scope(config, client=bad)
        # Every kind returned an empty (non-skipped) result.
        for kind in config.shared_roots:
            assert results[kind].installed == []
            assert results[kind].uninstalled == []
        # And the previously installed skill is still on disk.
        assert (Path(config.shared_roots["skill"]) / "lint" / "SKILL.md").exists()

    def test_tombstoned_item_still_installed_with_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(
                    kind="skill", slug="retired",
                    deleted_at="2026-01-01T00:00:00Z",
                ),
            ]),
        )
        results = sync_agent_scope(config, client=resolver)
        assert "retired" in results["skill"].installed
        assert "retired" in results["skill"].skipped_tombstoned


# ── sync_task_scope() — Phase 2 task-claim sync ──────────────────────


class TestSyncTaskScope:
    def test_feature_flag_off_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _disable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver()
        result = sync_task_scope(config, "task-1", client=resolver)
        assert result.skipped is True
        assert result.sandbox_dir is None
        assert resolver.calls == []
        # Teardown is always callable, even when skipped.
        result.teardown()

    def test_no_task_overrides_returns_no_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            task_scopes={
                "task-1": _resolved_envelope([
                    # Only an agent-scoped item — Phase 2 should ignore.
                    _make_resolved_item_dict(kind="skill", slug="lint", scope="agent"),
                ]),
            },
        )
        result = sync_task_scope(config, "task-1", client=resolver)
        assert result.skipped is False
        assert result.sandbox_dir is None
        assert result.materialised == []
        # Sandbox dir was NOT created — nothing to materialise.
        assert not (Path(config.sandbox_root) / "task-1").exists()

    def test_materialises_task_scoped_items(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            task_scopes={
                "task-XYZ": _resolved_envelope([
                    _make_resolved_item_dict(
                        kind="skill", slug="taskonly",
                        scope="task",
                        payload=_skill_payload("task-scoped skill body"),
                    ),
                    _make_resolved_item_dict(
                        kind="subagent", slug="reviewer",
                        scope="task",
                        revision_id="rev-7",
                        attachment_id="att-9",
                    ),
                    # Agent-scope items must not leak into the sandbox.
                    _make_resolved_item_dict(
                        kind="skill", slug="lint", scope="agent",
                    ),
                ]),
            },
        )
        result = sync_task_scope(config, "task-XYZ", client=resolver)
        assert result.sandbox_dir is not None
        assert {m.slug for m in result.materialised} == {"taskonly", "reviewer"}

        sandbox = Path(config.sandbox_root) / "task-XYZ"
        skill_md = sandbox / "skill" / "taskonly" / "SKILL.md"
        assert skill_md.read_text() == "task-scoped skill body"
        sub_md = sandbox / "subagent" / "reviewer" / "reviewer.md"
        assert sub_md.exists()
        # Agent-scope leak check — `lint` must not be present.
        assert not (sandbox / "skill" / "lint").exists()

    def test_teardown_removes_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            task_scopes={
                "task-T": _resolved_envelope([
                    _make_resolved_item_dict(
                        kind="skill", slug="taskonly", scope="task",
                    ),
                ]),
            },
        )
        result = sync_task_scope(config, "task-T", client=resolver)
        sandbox = result.sandbox_dir
        assert sandbox is not None and sandbox.exists()
        result.teardown()
        assert not sandbox.exists()
        # Teardown is idempotent — calling it a second time is a no-op.
        result.teardown()

    def test_fetch_failure_returns_skipped_no_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(raise_on={"task-bad"})
        result = sync_task_scope(config, "task-bad", client=resolver)
        assert result.skipped is True
        assert result.sandbox_dir is None
        # Teardown still callable.
        result.teardown()

    def test_concurrent_task_sandboxes_are_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            task_scopes={
                "task-A": _resolved_envelope([
                    _make_resolved_item_dict(
                        kind="skill", slug="taskonly",
                        scope="task",
                        payload=_skill_payload("A's version"),
                        attachment_id="att-A",
                    ),
                ]),
                "task-B": _resolved_envelope([
                    _make_resolved_item_dict(
                        kind="skill", slug="taskonly",
                        scope="task",
                        payload=_skill_payload("B's version"),
                        attachment_id="att-B",
                    ),
                ]),
            },
        )
        a = sync_task_scope(config, "task-A", client=resolver)
        b = sync_task_scope(config, "task-B", client=resolver)
        # Each task gets its own copy under its own sandbox dir.
        a_text = (a.sandbox_dir / "skill" / "taskonly" / "SKILL.md").read_text()
        b_text = (b.sandbox_dir / "skill" / "taskonly" / "SKILL.md").read_text()
        assert a_text == "A's version"
        assert b_text == "B's version"
        # Tearing down B must not affect A.
        b.teardown()
        assert a.sandbox_dir.exists()
        assert (a.sandbox_dir / "skill" / "taskonly" / "SKILL.md").exists()

    def test_rejects_task_scope_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
    ):
        # The server should reject scope=task+kind=module at write time
        # (proposal §8 v1 restriction); defence-in-depth client guard.
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            task_scopes={
                "task-M": _resolved_envelope([
                    _make_resolved_item_dict(
                        kind="module", slug="forbidden", scope="task",
                        payload={"manifest": {}, "install": {"steps": []}},
                    ),
                ]),
            },
        )
        result = sync_task_scope(config, "task-M", client=resolver)
        assert result.materialised == []
        # No sandbox was created because no legitimate overrides remained.
        assert result.sandbox_dir is None


# ── materialise_items() — used directly by tests + future loaders ────


class TestMaterialiseItems:
    def test_flat_layout(self, tmp_path: Path):
        items = [
            ResolvedItem.from_api(
                _make_resolved_item_dict(kind="skill", slug="lint"),
            ),
        ]
        installed = materialise_items(items, target_root=tmp_path, layout="flat")
        assert installed[0] == tmp_path / "lint"
        assert (tmp_path / "lint" / "SKILL.md").exists()

    def test_by_kind_layout(self, tmp_path: Path):
        items = [
            ResolvedItem.from_api(
                _make_resolved_item_dict(kind="skill", slug="lint"),
            ),
            ResolvedItem.from_api(
                _make_resolved_item_dict(kind="subagent", slug="coder"),
            ),
        ]
        materialise_items(items, target_root=tmp_path, layout="by-kind")
        assert (tmp_path / "skill" / "lint" / "SKILL.md").exists()
        assert (tmp_path / "subagent" / "coder" / "coder.md").exists()


# ── Path-traversal defences ───────────────────────────────────────────


class TestPathTraversalDefences:
    """Refuse to materialise items whose slug/task_id would escape root.

    Triggered by the security blocker in the PR review: a malicious
    resolver payload (or a bug upstream) could ship ``slug='../escape'``
    and reach ``rmtree`` / ``write_text`` outside the agent-owned root.
    """

    @pytest.mark.parametrize(
        "bad_slug",
        ["../escape", "../../etc/passwd", "a/b", "..", ".", "", "with\x00null"],
    )
    def test_materialise_rejects_unsafe_slug(self, tmp_path: Path, bad_slug: str):
        item = ResolvedItem.from_api(
            _make_resolved_item_dict(kind="skill", slug=bad_slug or "x"),
        )
        # Force the slug post-construction so ResolvedItem accepts it
        # (no client-side validation today on raw bytes) — the helper
        # is the boundary we care about.
        object.__setattr__(item, "slug", bad_slug)
        with pytest.raises(UnsafePathSegmentError):
            materialise_items([item], target_root=tmp_path, layout="flat")
        # Nothing escaped: tmp_path's parent must still be untouched.
        assert list(tmp_path.iterdir()) == []

    def test_agent_scope_skips_unsafe_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(kind="skill", slug="../escape"),
                _make_resolved_item_dict(kind="skill", slug="lint"),
            ]),
        )
        results = sync_agent_scope(config, client=resolver)
        # Only the safe slug landed; the traversal attempt did not.
        assert results["skill"].installed == ["lint"]
        skill_root = Path(config.shared_roots["skill"])
        # The shared root contains only the legitimate install.
        assert sorted(p.name for p in skill_root.iterdir()) == ["lint"]
        # And no sibling of the shared root was created — i.e. nothing
        # was written via ``shared_root/../escape``.
        assert sorted(p.name for p in skill_root.parent.iterdir()) == [
            "modules", "skills", "subagents",
        ]

    def test_task_scope_rejects_unsafe_task_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            task_scopes={
                "../escape": _resolved_envelope([
                    _make_resolved_item_dict(
                        kind="skill", slug="taskonly", scope="task",
                    ),
                ]),
            },
        )
        result = sync_task_scope(config, "../escape", client=resolver)
        assert result.skipped is True
        assert result.sandbox_dir is None
        # The resolver was never called — we refused before issuing HTTP.
        assert resolver.calls == []
        # Sandbox root parent must not contain an "escape" sibling.
        assert not (Path(config.sandbox_root).parent / "escape").exists()
        # Teardown stays callable + no-op.
        result.teardown()


# ── resolve_path() round-trips the subagent layout sync writes ───────


class TestResolvePathSubagentRoundTrip:
    """A subagent installed by sync_agent_scope must be reachable via resolve_path.

    Blocker from the PR review: sync writes ``<shared>/<slug>/<slug>.md``
    but the original lookup returned the per-item directory because the
    dir exists first.  Any loader that opens the returned path would
    hit the directory and fail.
    """

    def test_finds_subagent_md_inside_per_item_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(kind="subagent", slug="coder"),
            ]),
        )
        sync_agent_scope(config, client=resolver)

        found = resolve_path(
            "subagent", "coder",
            shared_root=config.shared_roots["subagent"],
        )
        assert found is not None
        # Must point at the markdown file, not the per-item directory.
        assert found.is_file()
        assert found.name == "coder.md"
        # And the content must be the rendered subagent body, so a
        # consumer can read it straight off the returned path.
        text = found.read_text()
        assert "name: coder" in text

    def test_finds_subagent_via_task_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        # Install an agent-scope subagent that lives in the shared root.
        agent_resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(
                    kind="subagent", slug="coder",
                    payload=_subagent_payload(body="shared body"),
                ),
            ]),
        )
        sync_agent_scope(config, client=agent_resolver)

        # Materialise a task-scoped override of the same slug.
        task_resolver = FakeResolver(
            task_scopes={
                "task-X": _resolved_envelope([
                    _make_resolved_item_dict(
                        kind="subagent", slug="coder",
                        scope="task",
                        attachment_id="att-task",
                        payload=_subagent_payload(body="task body"),
                    ),
                ]),
            },
        )
        sync_task_scope(config, "task-X", client=task_resolver)

        found = resolve_path(
            "subagent", "coder",
            shared_root=config.shared_roots["subagent"],
            task_id="task-X",
            sandbox_root=config.sandbox_root,
        )
        assert found is not None
        # Task overlay wins, and the returned path is the .md file
        # inside the per-item dir of the task sandbox.
        assert found.is_file()
        assert "task body" in found.read_text()

    def test_unsafe_slug_returns_none(self, tmp_path: Path):
        # resolve_path must also refuse traversal — it's a public
        # helper that takes raw strings from arbitrary callers.
        (tmp_path / "shared").mkdir()
        assert resolve_path(
            "subagent", "../escape",
            shared_root=str(tmp_path / "shared"),
        ) is None


# ── Module manifest round-trip (mcp field preserved) ──────────────────


class TestModuleManifestRoundTrip:
    """The synced module.yaml must contain every field module_loader reads.

    Blocker from the PR review: the original installer only persisted
    ``description`` and ``env``, silently dropping ``mcp`` — which
    means a registry-synced module would lose its MCP server bindings
    relative to a hand-installed copy.
    """

    @staticmethod
    def _module_payload(*, mcp: dict | None = None) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "description": "Knowledge module",
            "env_keys": ["SUPERPOS_API_TOKEN", "SUPERPOS_HIVE_ID"],
        }
        if mcp is not None:
            manifest["mcp"] = mcp
        return {"manifest": manifest, "skill": "Module SKILL body\n"}

    def test_mcp_config_round_trips_through_module_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        mcp_cfg = {
            "knowledge-mcp": {
                "command": "knowledge-mcp",
                "args": ["--stdio"],
                "env": {"SUPERPOS_API_TOKEN": "${SUPERPOS_API_TOKEN}"},
            },
        }
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(
                    kind="module", slug="knowledge",
                    payload=self._module_payload(mcp=mcp_cfg),
                ),
            ]),
        )
        sync_agent_scope(config, client=resolver)
        module_yaml = Path(config.shared_roots["module"]) / "knowledge" / "module.yaml"
        assert module_yaml.exists()
        data = yaml.safe_load(module_yaml.read_text())
        # description + env still present.
        assert data["description"] == "Knowledge module"
        assert data["env"] == ["SUPERPOS_API_TOKEN", "SUPERPOS_HIVE_ID"]
        # mcp config preserved verbatim — this is the regression guard.
        assert data["mcp"] == mcp_cfg

    def test_module_loader_reads_synced_mcp_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # End-to-end: sync writes the module → module_loader picks up
        # the same MCP config that ``collect_mcp_servers`` would merge.
        from superpos_agent_core import module_loader

        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        mcp_cfg = {"my-mcp": {"command": "echo", "args": ["hi"]}}
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(
                    kind="module", slug="hello",
                    payload=self._module_payload(mcp=mcp_cfg),
                ),
            ]),
        )
        sync_agent_scope(config, client=resolver)
        modules = module_loader.discover_modules(
            config.shared_roots["module"], include_bundled=False,
        )
        names = [m.name for m in modules]
        assert "hello" in names
        hello = next(m for m in modules if m.name == "hello")
        assert hello.has_mcp is True
        assert hello.mcp_config == mcp_cfg
        # And the merged collector returns the same payload.
        assert module_loader.collect_mcp_servers(modules) == mcp_cfg

    def test_module_without_mcp_field_omits_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Symmetry: a module whose manifest lacks ``mcp`` should not
        # have a stray ``mcp: null`` written into module.yaml — that
        # would set ``has_mcp=False`` correctly today, but tomorrow's
        # loader checks should not have to special-case the value.
        _enable_flag(monkeypatch)
        config = _config(tmp_path)
        resolver = FakeResolver(
            agent_scope=_resolved_envelope([
                _make_resolved_item_dict(
                    kind="module", slug="plain",
                    payload=self._module_payload(mcp=None),
                ),
            ]),
        )
        sync_agent_scope(config, client=resolver)
        data = yaml.safe_load(
            (Path(config.shared_roots["module"]) / "plain" / "module.yaml").read_text(),
        )
        assert "mcp" not in data
