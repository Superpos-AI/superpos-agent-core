"""Tests for sub_agent_sync — SubAgentDefinition → local subagent file sync."""

from __future__ import annotations

from pathlib import Path

import pytest

from superpos_agent_core.sub_agent_sync import (
    MANAGED_MARKER,
    assemble_prompt,
    build_subagent_md,
    discover_local_context,
    sync_sub_agents,
    _get_document_content,
)


class TestGetDocumentContent:
    def test_string(self):
        assert _get_document_content("hello") == "hello"

    def test_empty_string(self):
        assert _get_document_content("") is None

    def test_whitespace_only(self):
        assert _get_document_content("   ") is None

    def test_none(self):
        assert _get_document_content(None) is None

    def test_dict_with_content(self):
        assert _get_document_content({"content": "hello"}) == "hello"

    def test_dict_empty_content(self):
        assert _get_document_content({"content": ""}) is None

    def test_dict_no_content_key(self):
        assert _get_document_content({"locked": True}) is None


class TestAssemblePrompt:
    def test_all_documents(self):
        docs = {
            "SOUL": "identity",
            "AGENT": "workflow",
            "RULES": "constraints",
            "STYLE": "tone",
            "EXAMPLES": "few-shot",
            "NOTES": "misc",
        }
        result = assemble_prompt(docs)
        assert "# SOUL\n\nidentity" in result
        assert "# NOTES\n\nmisc" in result

    def test_skips_empty(self):
        docs = {"SOUL": "identity", "RULES": "", "NOTES": None}
        result = assemble_prompt(docs)
        assert "# SOUL" in result
        assert "# RULES" not in result
        assert "# NOTES" not in result

    def test_preserves_order(self):
        docs = {"NOTES": "last", "SOUL": "first"}
        result = assemble_prompt(docs)
        soul_pos = result.index("# SOUL")
        notes_pos = result.index("# NOTES")
        assert soul_pos < notes_pos

    def test_object_format_documents(self):
        docs = {"SOUL": {"content": "identity", "locked": True}}
        result = assemble_prompt(docs)
        assert "# SOUL\n\nidentity" in result


class TestBuildSubagentMd:
    def test_basic_output(self):
        defn = {
            "slug": "coder",
            "name": "Coding Agent",
            "description": "Writes code",
            "model": "claude-opus-4-6",
            "version": 2,
            "documents": {"SOUL": "You write code."},
        }
        result = build_subagent_md(defn)
        assert "name: coder" in result
        assert 'description: "Coding Agent — Writes code"' in result
        assert "model: claude-opus-4-6" in result
        assert "# SOUL\n\nYou write code." in result
        assert MANAGED_MARKER in result

    def test_no_model_uses_config(self):
        defn = {
            "slug": "test",
            "name": "Test",
            "version": 1,
            "config": {"llm": {"model": "claude-sonnet-4-6"}},
            "documents": {},
        }
        result = build_subagent_md(defn)
        assert "model: claude-sonnet-4-6" in result

    def test_allowed_tools(self):
        defn = {
            "slug": "test",
            "name": "Test",
            "version": 1,
            "documents": {},
            "allowed_tools": ["Read", "Grep"],
        }
        result = build_subagent_md(defn)
        assert "`Read`" in result
        assert "`Grep`" in result

    def test_memory_injection(self):
        defn = {"slug": "test", "name": "Test", "version": 1, "documents": {}}
        result = build_subagent_md(defn, memory="Project uses React 19.")
        assert "## Agent Memory" in result
        assert "Project uses React 19." in result

    def test_local_context_injection(self):
        defn = {"slug": "test", "name": "Test", "version": 1, "documents": {}}
        result = build_subagent_md(defn, local_context="**Installed modules**:\n- `github-pr`")
        assert "## Agent Capabilities" in result
        assert "`github-pr`" in result

    def test_version_comment_in_frontmatter(self):
        defn = {"slug": "test", "name": "Test", "version": 5, "documents": {}}
        result = build_subagent_md(defn)
        assert "# synced from Superpos SubAgentDefinition v5" in result


class TestDiscoverLocalContext:
    def test_subagent_slugs(self):
        result = discover_local_context(None, None, ["coder", "reviewer"])
        assert "`coder`" in result
        assert "`reviewer`" in result
        assert "Sibling subagents" in result

    def test_no_context(self):
        result = discover_local_context(None, None, [])
        assert result == ""

    def test_modules_dir(self, tmp_path):
        mod = tmp_path / "my-module"
        mod.mkdir()
        (mod / "module.yaml").write_text("description: test")
        result = discover_local_context(str(tmp_path), None, [])
        assert "`my-module`" in result

    def test_skills_dir(self, tmp_path):
        (tmp_path / "plan.md").write_text("---\nname: plan\n---\n")
        result = discover_local_context(None, str(tmp_path), [])
        assert "`/plan`" in result


class TestSyncSubAgents:
    def test_writes_definition_files(self, tmp_path):
        """Pre-fetched definitions should be written to disk."""
        definitions = [
            {
                "slug": "coder",
                "name": "Coder",
                "version": 1,
                "documents": {"SOUL": "You code."},
            },
        ]
        count = sync_sub_agents(
            subagents_dir=str(tmp_path),
            base_url="http://fake",
            token="fake",
            definitions=definitions,
        )
        assert count == 1
        coder_file = tmp_path / "coder.md"
        assert coder_file.exists()
        content = coder_file.read_text()
        assert "name: coder" in content
        assert MANAGED_MARKER in content

    def test_cleanup_stale_managed_files(self, tmp_path):
        """Managed files not in definitions should be deleted."""
        stale = tmp_path / "old-agent.md"
        stale.write_text(f"---\nname: old-agent\n---\nStale.\n\n{MANAGED_MARKER}\n")

        local = tmp_path / "my-local.md"
        local.write_text("---\nname: my-local\n---\nLocal only.\n")

        sync_sub_agents(
            subagents_dir=str(tmp_path),
            base_url="http://fake",
            token="fake",
            definitions=[],
        )

        assert not stale.exists(), "Stale managed file should be deleted"
        assert local.exists(), "Local (unmanaged) file should be preserved"

    def test_preserves_local_files(self, tmp_path):
        """Files without the managed marker should never be touched."""
        local = tmp_path / "custom.md"
        local.write_text("My custom subagent config")

        sync_sub_agents(
            subagents_dir=str(tmp_path),
            base_url="http://fake",
            token="fake",
            definitions=[],
        )

        assert local.exists()
        assert local.read_text() == "My custom subagent config"

    def test_memory_not_injected_without_flag(self, tmp_path):
        """When inject_memory=False, memory should not appear even if passed."""
        definitions = [
            {"slug": "test", "name": "Test", "version": 1, "documents": {}},
        ]
        sync_sub_agents(
            subagents_dir=str(tmp_path),
            base_url="http://fake",
            token="fake",
            definitions=definitions,
            memory="secret memory",
            inject_memory=False,
        )
        content = (tmp_path / "test.md").read_text()
        assert "secret memory" not in content

    def test_memory_injected_with_flag(self, tmp_path):
        """When inject_memory=True, memory should appear in output."""
        definitions = [
            {"slug": "test", "name": "Test", "version": 1, "documents": {}},
        ]
        sync_sub_agents(
            subagents_dir=str(tmp_path),
            base_url="http://fake",
            token="fake",
            definitions=definitions,
            memory="important context",
            inject_memory=True,
        )
        content = (tmp_path / "test.md").read_text()
        assert "important context" in content
