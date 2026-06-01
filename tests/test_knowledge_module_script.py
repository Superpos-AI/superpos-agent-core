"""Unit tests for the bundled superpos-knowledge CLI write logic.

The script ships without a ``.py`` extension (it's a PATH executable), so we
load it via importlib from the bundled modules dir and exercise its pure
helpers — value assembly, scope defaulting, and create/update arg parsing —
without any network.
"""

from __future__ import annotations

import argparse
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from superpos_agent_core import bundled_modules_dir

_SCRIPT = (
    Path(bundled_modules_dir())
    / "superpos-knowledge" / "scripts" / "superpos-knowledge"
)


def _load_script():
    # The script has no .py suffix, so an explicit source loader is needed —
    # spec_from_file_location can't infer one from the extension.
    loader = SourceFileLoader("_superpos_knowledge_cli", str(_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _ns(**kw) -> argparse.Namespace:
    base = dict(
        value=None, title=None, summary=None, content=None,
        tags=None, confidence=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_script_exists_and_is_executable():
    assert _SCRIPT.is_file()
    assert _SCRIPT.stat().st_mode & 0o111  # at least one execute bit


def test_build_value_stamps_provenance_metadata():
    mod = _load_script()
    value = mod._build_value(_ns(content="Rule: x", title="T", summary="S"))
    assert value["content"] == "Rule: x"
    assert value["title"] == "T"
    assert value["summary"] == "S"
    assert value["metadata"]["source"] == "agent_inline"
    assert value["metadata"]["auto_generated"] is True


def test_build_value_splits_tags_and_keeps_confidence():
    mod = _load_script()
    value = mod._build_value(_ns(content="c", tags="a, b ,c", confidence="high"))
    assert value["tags"] == ["a", "b", "c"]
    assert value["confidence"] == "high"


def test_build_value_raw_value_is_base_and_flags_override():
    mod = _load_script()
    value = mod._build_value(_ns(value='{"title": "old", "extra": 1}', title="new"))
    assert value["title"] == "new"      # structured flag wins
    assert value["extra"] == 1          # untouched raw field preserved
    assert value["metadata"]["source"] == "agent_inline"


def test_build_value_enforces_provenance_over_caller():
    mod = _load_script()
    value = mod._build_value(_ns(
        value='{"metadata": {"source": "custom", "auto_generated": false, "extra": "kept"}}',
        content="c",
    ))
    # Provenance fields are always overwritten regardless of caller input.
    assert value["metadata"]["source"] == "agent_inline"
    assert value["metadata"]["auto_generated"] is True
    # Other caller-supplied metadata fields survive.
    assert value["metadata"]["extra"] == "kept"


def test_default_scope_precedence(monkeypatch):
    mod = _load_script()
    assert mod._default_scope("agent:42") == "agent:42"           # explicit wins
    monkeypatch.setenv("SUPERPOS_KNOWLEDGE_FILLIN_SCOPE", "apiary")
    assert mod._default_scope(None) == "apiary"                   # env fallback
    monkeypatch.delenv("SUPERPOS_KNOWLEDGE_FILLIN_SCOPE", raising=False)
    assert mod._default_scope(None) == "hive"                     # final default


def test_parser_create_requires_key_and_content():
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["create"])  # missing --key and --content
    # valid create parses
    args = parser.parse_args(["create", "--key", "k", "--content", "c"])
    assert args.cmd == "create" and args.key == "k" and args.content == "c"


def test_parser_update_takes_entry_id_and_optional_content():
    mod = _load_script()
    parser = mod._build_parser()
    args = parser.parse_args(["update", "01ABC", "--summary", "s"])
    assert args.cmd == "update" and args.entry_id == "01ABC"
    assert args.content is None  # not required on update


@pytest.mark.asyncio
async def test_update_partial_flags_preserve_existing_fields(monkeypatch):
    """A partial update (e.g. only --summary) must not drop existing fields."""
    mod = _load_script()

    existing_entry = {
        "id": "01ABC",
        "value": {
            "title": "Original title",
            "summary": "Original summary",
            "content": "Rule: original content",
            "tags": ["tag1", "tag2"],
            "confidence": "high",
            "metadata": {
                "source": "knowledge_fillin",
                "auto_generated": True,
                "custom_field": "preserved",
            },
        },
    }

    mock_get = AsyncMock(return_value=existing_entry)
    mock_update = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["update", "01ABC", "--summary", "New summary"])
        args.sort = None
        await mod._run(args)

    sent_value = mock_update.call_args.kwargs["value"]
    assert sent_value["summary"] == "New summary"
    assert sent_value["title"] == "Original title"
    assert sent_value["content"] == "Rule: original content"
    assert sent_value["tags"] == ["tag1", "tag2"]
    assert sent_value["confidence"] == "high"
    assert sent_value["metadata"]["custom_field"] == "preserved"
    # source is re-stamped to agent_inline (correct: this write IS from the CLI)
    assert sent_value["metadata"]["source"] == "agent_inline"


@pytest.mark.asyncio
async def test_update_full_value_flag_replaces_all_fields(monkeypatch):
    """When --value supplies a full JSON object without individual flags,
    it fully replaces existing fields — old fields must disappear and
    get_knowledge is NOT called (no read-modify-write)."""
    mod = _load_script()

    existing_entry = {
        "id": "01ABC",
        "value": {
            "title": "Old",
            "content": "Old content",
            "summary": "Old summary",
            "tags": ["old_tag"],
            "custom_field": "should_disappear",
            "metadata": {"source": "knowledge_fillin", "custom_field": "old_meta"},
        },
    }

    mock_get = AsyncMock(return_value=existing_entry)
    mock_update = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    full_json = '{"title": "Brand new", "content": "Brand new content", "tags": ["fresh"]}'
    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["update", "01ABC", "--value", full_json])
        args.sort = None
        await mod._run(args)

    # get_knowledge should NOT be called — full replacement skips read-modify-write
    mock_get.assert_not_called()

    sent_value = mock_update.call_args.kwargs["value"]
    assert sent_value["title"] == "Brand new"
    assert sent_value["content"] == "Brand new content"
    assert sent_value["tags"] == ["fresh"]
    # Old fields must be gone
    assert "summary" not in sent_value
    assert "custom_field" not in sent_value
    # Provenance metadata is stamped
    assert sent_value["metadata"]["source"] == "agent_inline"
    assert sent_value["metadata"]["auto_generated"] is True
    # Old metadata fields must be gone (no merge with existing)
    assert "custom_field" not in sent_value.get("metadata", {})


@pytest.mark.asyncio
async def test_update_value_with_flag_overrides_still_merges(monkeypatch):
    """When --value is combined with individual flags (e.g. --summary),
    the read-modify-write merge path is still used."""
    mod = _load_script()

    existing_entry = {
        "id": "01ABC",
        "value": {
            "title": "old",
            "summary": "old summary",
            "content": "old content",
            "metadata": {"source": "knowledge_fillin"},
        },
    }

    mock_get = AsyncMock(return_value=existing_entry)
    mock_update = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC",
            "--value", '{"title": "base"}',
            "--summary", "new summary",
        ])
        args.sort = None
        await mod._run(args)

    # get_knowledge IS called because individual flags trigger read-modify-write
    mock_get.assert_called_once()

    sent_value = mock_update.call_args.kwargs["value"]
    # --value provides title="base", --summary overrides summary
    assert sent_value["title"] == "base"
    assert sent_value["summary"] == "new summary"
    # Existing field "content" is preserved via merge
    assert sent_value["content"] == "old content"
    # Provenance metadata stamped
    assert sent_value["metadata"]["source"] == "agent_inline"
    assert sent_value["metadata"]["auto_generated"] is True
