"""Unit tests for the bundled superpos-knowledge CLI write logic.

The script ships without a ``.py`` extension (it's a PATH executable), so we
load it via importlib from the bundled modules dir and exercise its pure
helpers — value assembly, scope defaulting, and create/update arg parsing —
without any network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
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
        type=None, slug=None, body=None, body_file=None,
        frontmatter=None, source_ids=None,
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


@pytest.mark.asyncio
async def test_create_legacy_without_key_or_content_returns_2(monkeypatch):
    """Legacy create with neither --key nor --type must exit 2 (no network)."""
    mod = _load_script()

    mock_create_page = AsyncMock()
    mock_create_legacy = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "create_knowledge", mock_create_legacy), \
         patch.object(mod.SuperposClient, "close", mock_close):
        # No --key and no --type: legacy validation in _run must reject
        args = mod._build_parser().parse_args(["create"])
        args.sort = None
        rc = await mod._run(args)
    assert rc == 2
    mock_create_page.assert_not_called()
    mock_create_legacy.assert_not_called()


def test_parser_update_takes_entry_id_and_optional_content():
    mod = _load_script()
    parser = mod._build_parser()
    args = parser.parse_args(["update", "01ABC", "--summary", "s"])
    assert args.cmd == "update" and args.entry_id == "01ABC"
    assert args.content is None  # not required on update


@pytest.mark.asyncio
async def test_update_partial_flags_preserve_existing_fields(monkeypatch):
    """A legacy partial update (e.g. only --content) must not drop existing fields.

    --content is a legacy-only flag, so this stays on the read-modify-write
    legacy path. (Shared metadata-only flags like --summary/--tags now route to
    the typed page path — see test_update_shared_flags_route_to_typed_page.)
    """
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
        args = mod._build_parser().parse_args(
            ["update", "01ABC", "--content", "Rule: new content"]
        )
        args.sort = None
        await mod._run(args)

    sent_value = mock_update.call_args.kwargs["value"]
    assert sent_value["content"] == "Rule: new content"
    assert sent_value["title"] == "Original title"
    assert sent_value["summary"] == "Original summary"
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


# ── Typed-page shape (TASK-297) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_typed_routes_to_create_knowledge_page(monkeypatch):
    """create --type ... --slug ... --body ... must call create_knowledge_page,
    not the legacy create_knowledge."""
    mod = _load_script()

    mock_create_page = AsyncMock(return_value={"id": "01NEW", "type": "topic", "slug": "s"})
    mock_create_legacy = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "create_knowledge", mock_create_legacy), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "s", "--body", "b",
        ])
        args.sort = None
        await mod._run(args)

    mock_create_page.assert_awaited_once()
    kwargs = mock_create_page.call_args.kwargs
    assert kwargs["type"] == "topic"
    assert kwargs["slug"] == "s"
    assert kwargs["body"] == "b"
    assert kwargs["scope"] == "hive"  # default scope
    # Legacy path must NOT be called
    mock_create_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_create_typed_with_body_file_reads_file(monkeypatch, tmp_path):
    mod = _load_script()
    body_file = tmp_path / "page.md"
    body_file.write_text("# From file\n\nMarkdown body.", encoding="utf-8")

    mock_create_page = AsyncMock(return_value={"id": "01NEW"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "s",
            "--body-file", str(body_file),
        ])
        args.sort = None
        await mod._run(args)

    assert mock_create_page.call_args.kwargs["body"] == "# From file\n\nMarkdown body."


@pytest.mark.asyncio
async def test_create_typed_rejects_unknown_type(monkeypatch):
    """Unknown --type values must exit 2 (via _build_typed_page_kwargs)."""
    mod = _load_script()
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")
    mock_create_page = AsyncMock()
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "badtype", "--slug", "s", "--body", "b",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)
    assert exc.value.code == 2
    mock_create_page.assert_not_called()


@pytest.mark.asyncio
async def test_create_typed_rejects_missing_body(monkeypatch):
    """--type without --body/--body-file must exit 2."""
    mod = _load_script()
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")
    mock_create_page = AsyncMock()
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["create", "--type", "topic", "--slug", "s"])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)
    assert exc.value.code == 2
    mock_create_page.assert_not_called()


@pytest.mark.asyncio
async def test_create_typed_rejects_missing_slug(monkeypatch):
    """--type without --slug must exit 2 (typed contract is type+slug+body) and
    never reach the client with slug=None."""
    mod = _load_script()
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")
    mock_create_page = AsyncMock()
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["create", "--type", "topic", "--body", "b"])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)
    assert exc.value.code == 2
    mock_create_page.assert_not_called()


@pytest.mark.asyncio
async def test_create_typed_and_legacy_are_mutually_exclusive(monkeypatch):
    """Passing both --key (legacy) and --type (typed) must exit 2 and never
    hit the network."""
    mod = _load_script()

    mock_create_page = AsyncMock()
    mock_create_legacy = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "create_knowledge", mock_create_legacy), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create",
            "--key", "k",
            "--type", "topic", "--slug", "s", "--body", "b",
            "--content", "c",
        ])
        args.sort = None
        rc = await mod._run(args)
    assert rc == 2
    mock_create_page.assert_not_called()
    mock_create_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_create_typed_rejects_legacy_only_flags(monkeypatch):
    """Typed (--type) create must reject legacy-only flags like --content."""
    mod = _load_script()

    mock_create_page = AsyncMock()
    mock_create_legacy = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "create_knowledge", mock_create_legacy), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create",
            "--type", "topic", "--slug", "s", "--body", "typed",
            "--content", "legacy",
        ])
        args.sort = None
        rc = await mod._run(args)
    assert rc == 2
    mock_create_page.assert_not_called()
    mock_create_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_create_legacy_rejects_typed_only_flags(monkeypatch):
    """Legacy (--key) create must reject typed-only flags like --body."""
    mod = _load_script()

    mock_create_page = AsyncMock()
    mock_create_legacy = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "create_knowledge", mock_create_legacy), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create",
            "--key", "k", "--content", "c", "--body", "typed",
        ])
        args.sort = None
        rc = await mod._run(args)
    assert rc == 2
    mock_create_legacy.assert_not_called()
    mock_create_page.assert_not_called()


@pytest.mark.asyncio
async def test_create_typed_rejects_empty_body_with_body_file(monkeypatch, tmp_path):
    """An explicit empty --body alongside --body-file must be rejected by mutual
    exclusion (exit 2), not silently drop --body-file. Regression: the presence
    check uses `is not None`, so a falsy empty string still counts as supplied."""
    mod = _load_script()
    body_file = tmp_path / "page.md"
    body_file.write_text("# From file", encoding="utf-8")

    mock_create_page = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "s",
            "--body", "", "--body-file", str(body_file),
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)
    assert exc.value.code == 2
    mock_create_page.assert_not_called()


@pytest.mark.asyncio
async def test_update_rejects_empty_body_with_body_file(monkeypatch, tmp_path):
    """The update path shares _resolve_body, so an explicit empty --body with
    --body-file must also be rejected (exit 2) and never hit the network."""
    mod = _load_script()
    body_file = tmp_path / "page.md"
    body_file.write_text("# From file", encoding="utf-8")

    mock_update_page = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ENTRY",
            "--body", "", "--body-file", str(body_file),
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)
    assert exc.value.code == 2
    mock_update_page.assert_not_called()


@pytest.mark.asyncio
async def test_create_legacy_rejects_empty_string_typed_only_flag(monkeypatch):
    """A legacy (--key) create with an explicit empty typed-only flag
    (`--body ""`) must be rejected (exit 2), not silently dropped."""
    mod = _load_script()

    mock_create_page = AsyncMock()
    mock_create_legacy = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "create_knowledge", mock_create_legacy), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--key", "k", "--content", "c", "--body", "",
        ])
        args.sort = None
        rc = await mod._run(args)
    assert rc == 2
    mock_create_legacy.assert_not_called()
    mock_create_page.assert_not_called()


@pytest.mark.asyncio
async def test_create_typed_rejects_empty_string_legacy_only_flag(monkeypatch):
    """A typed (--type) create with an explicit empty legacy-only flag
    (`--content ""`) must be rejected (exit 2), not silently dropped."""
    mod = _load_script()

    mock_create_page = AsyncMock()
    mock_create_legacy = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "create_knowledge", mock_create_legacy), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "s", "--body", "b",
            "--content", "",
        ])
        args.sort = None
        rc = await mod._run(args)
    assert rc == 2
    mock_create_page.assert_not_called()
    mock_create_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_create_typed_with_frontmatter_parses_json(monkeypatch):
    mod = _load_script()

    mock_create_page = AsyncMock(return_value={"id": "01NEW"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "s", "--body", "b",
            "--frontmatter", '{"summary": "x"}',
        ])
        args.sort = None
        await mod._run(args)

    assert mock_create_page.call_args.kwargs["frontmatter"] == {"summary": "x"}


@pytest.mark.asyncio
async def test_create_typed_does_not_auto_stamp_source_ids(monkeypatch):
    """``source_ids`` is gated by proposal §6.8 server-side; auto-stamping
    ``SUPERPOS_AGENT_ID`` would 403 for agents whose read scope doesn't
    cover their own page.  We only forward ``source_ids`` when the user
    explicitly opts in via ``--source-ids``.
    """
    mod = _load_script()

    mock_create_page = AsyncMock(return_value={"id": "01NEW"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")
    monkeypatch.setenv("SUPERPOS_AGENT_ID", "01AGENT")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "s", "--body", "b",
        ])
        args.sort = None
        await mod._run(args)

    # No auto-stamp — source_ids stays None unless --source-ids is given.
    assert mock_create_page.call_args.kwargs["source_ids"] is None


@pytest.mark.asyncio
async def test_create_typed_with_explicit_source_ids(monkeypatch):
    """When ``--source-ids`` is supplied, its value is forwarded verbatim."""
    mod = _load_script()

    mock_create_page = AsyncMock(return_value={"id": "01NEW"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")
    monkeypatch.setenv("SUPERPOS_AGENT_ID", "01AGENT")  # must NOT be used

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "s", "--body", "b",
            "--source-ids", "01SRC1,01SRC2",
        ])
        args.sort = None
        await mod._run(args)

    assert mock_create_page.call_args.kwargs["source_ids"] == ["01SRC1", "01SRC2"]


@pytest.mark.asyncio
async def test_update_typed_routes_to_update_knowledge_page(monkeypatch):
    mod = _load_script()

    mock_update_page = AsyncMock(return_value={"id": "01ABC", "body": "new"})
    mock_update_legacy = AsyncMock()
    mock_get = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--body", "new body",
        ])
        args.sort = None
        await mod._run(args)

    mock_update_page.assert_awaited_once()
    kwargs = mock_update_page.call_args
    assert kwargs.args[0] == "01ABC"
    assert kwargs.kwargs["body"] == "new body"
    mock_update_legacy.assert_not_called()
    mock_get.assert_not_called()  # no read-modify-write in typed path


@pytest.mark.asyncio
async def test_update_typed_with_body_file(monkeypatch, tmp_path):
    mod = _load_script()
    body_file = tmp_path / "body.md"
    body_file.write_text("file body content", encoding="utf-8")

    mock_update_page = AsyncMock(return_value={"id": "01ABC"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--body-file", str(body_file),
        ])
        args.sort = None
        await mod._run(args)

    assert mock_update_page.call_args.kwargs["body"] == "file body content"


@pytest.mark.asyncio
async def test_update_typed_partial_only_sends_supplied_fields(monkeypatch):
    """`update ID --body 'new'` must send ONLY body in the typed payload."""
    mod = _load_script()

    mock_update_page = AsyncMock(return_value={"id": "01ABC"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--body", "new body",
        ])
        args.sort = None
        await mod._run(args)

    kwargs = mock_update_page.call_args.kwargs
    assert kwargs["body"] == "new body"
    # Only `body` carries a value; visibility/ttl are passed through as None
    # and do not constitute a "supplied field" from the CLI's perspective.
    assert kwargs.get("visibility") is None
    assert kwargs.get("ttl") is None
    # No other field is supplied.
    for k in ("title", "summary", "frontmatter", "tags", "source_ids"):
        assert kwargs.get(k) is None, f"unexpected field {k!r}={kwargs.get(k)!r}"


@pytest.mark.asyncio
async def test_update_typed_rejects_type_change(monkeypatch):
    """--type on update must exit 2 (typed-only flag triggers the typed path)."""
    mod = _load_script()
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")
    mock_update_page = AsyncMock()
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--type", "procedure", "--body", "b",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)
    assert exc.value.code == 2
    mock_update_page.assert_not_called()


@pytest.mark.asyncio
async def test_update_typed_rejects_slug_change(monkeypatch):
    """--slug on update must exit 2 (typed-only flag triggers the typed path)."""
    mod = _load_script()
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")
    mock_update_page = AsyncMock()
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--slug", "new-slug", "--body", "b",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)
    assert exc.value.code == 2
    mock_update_page.assert_not_called()


@pytest.mark.asyncio
async def test_update_legacy_still_works(monkeypatch):
    """Regression: `update ID --content 'new'` must still call
    update_knowledge (legacy path), not the new typed method."""
    mod = _load_script()

    existing_entry = {
        "id": "01ABC",
        "value": {"title": "Old", "content": "Old content"},
    }
    mock_get = AsyncMock(return_value=existing_entry)
    mock_update = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_page = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update), \
         patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--content", "new content",
        ])
        args.sort = None
        await mod._run(args)

    mock_update.assert_awaited_once()
    mock_update_page.assert_not_called()


def test_parser_create_typed_minimal():
    """The minimal typed create invocation parses cleanly."""
    mod = _load_script()
    parser = mod._build_parser()
    args = parser.parse_args([
        "create", "--type", "topic", "--slug", "s", "--body", "b",
    ])
    assert args.cmd == "create"
    assert args.type == "topic"
    assert args.slug == "s"
    assert args.body == "b"
    assert args.body_file is None
    assert args.frontmatter is None
    assert args.source_ids is None
    assert args.title is None
    assert args.summary is None
    assert args.tags is None



# ── Update shape-routing regression (gilfoilbot-dev review, PR #31) ──────────
# The original dispatcher only entered the typed path on
# --body/--body-file/--frontmatter/--source-ids, so a metadata-only update
# (--title/--summary/--tags) silently fell through to the legacy `value` path
# (corrupting/no-opping typed page metadata) and --type/--slug alone were never
# rejected. These tests pin the corrected routing.


@pytest.mark.asyncio
async def test_update_shared_flags_route_to_typed_page(monkeypatch):
    """`update ID --title ... --tags ...` (no legacy-only flag) must call
    update_knowledge_page, not the legacy update_knowledge path."""
    mod = _load_script()

    mock_update_page = AsyncMock(return_value={"id": "01ABC"})
    mock_update_legacy = AsyncMock()
    mock_get = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--title", "New title", "--tags", "proposal,architecture",
        ])
        args.sort = None
        await mod._run(args)

    mock_update_page.assert_awaited_once()
    kwargs = mock_update_page.call_args
    assert kwargs.args[0] == "01ABC"
    assert kwargs.kwargs["title"] == "New title"
    assert kwargs.kwargs["tags"] == ["proposal", "architecture"]
    # No legacy value payload and no read-modify-write on the typed path.
    mock_update_legacy.assert_not_called()
    mock_get.assert_not_called()
    # Typed partial update sends only the supplied fields.
    assert "body" not in kwargs.kwargs
    assert "summary" not in kwargs.kwargs


@pytest.mark.asyncio
async def test_update_summary_only_routes_to_typed_page(monkeypatch):
    """A single shared metadata flag (--summary) is enough to take the typed
    path — it must not silently write a legacy value payload."""
    mod = _load_script()

    mock_update_page = AsyncMock(return_value={"id": "01ABC"})
    mock_update_legacy = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--summary", "New gist",
        ])
        args.sort = None
        await mod._run(args)

    mock_update_page.assert_awaited_once()
    assert mock_update_page.call_args.kwargs["summary"] == "New gist"
    mock_update_legacy.assert_not_called()


@pytest.mark.asyncio
async def test_update_type_alone_exits_2(monkeypatch):
    """`update ID --type procedure` (no other flag) must exit 2 without
    calling either update method — the original routing missed this."""
    mod = _load_script()

    mock_update_page = AsyncMock()
    mock_update_legacy = AsyncMock()
    mock_get = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--type", "procedure",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update_page.assert_not_called()
    mock_update_legacy.assert_not_called()
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_update_slug_alone_exits_2(monkeypatch):
    """`update ID --slug new-slug` (no other flag) must exit 2 without
    calling either update method — the original routing missed this."""
    mod = _load_script()

    mock_update_page = AsyncMock()
    mock_update_legacy = AsyncMock()
    mock_get = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--slug", "new-slug",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update_page.assert_not_called()
    mock_update_legacy.assert_not_called()
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_update_title_with_content_stays_legacy(monkeypatch):
    """A legacy-only flag (--content) pins the legacy path even when a shared
    metadata flag (--title) is also present, so existing legacy callers that
    combine the two keep their read-modify-write behavior."""
    mod = _load_script()

    existing_entry = {"id": "01ABC", "value": {"title": "Old", "content": "Old content"}}
    mock_get = AsyncMock(return_value=existing_entry)
    mock_update_legacy = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_page = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--title", "New title", "--content", "Rule: new",
        ])
        args.sort = None
        await mod._run(args)

    mock_update_legacy.assert_awaited_once()
    mock_update_page.assert_not_called()
    sent_value = mock_update_legacy.call_args.kwargs["value"]
    assert sent_value["title"] == "New title"
    assert sent_value["content"] == "Rule: new"


# ── Metadata-only update routing (visibility/ttl) regression, PR #31 ─────────
# --visibility/--ttl are metadata fields, NOT a writable content shape. A bare
# `update ID --visibility private` (no typed content flag, no legacy-only flag)
# must route through the legacy get_knowledge()+update_knowledge()
# read-modify-write path, which re-sends the existing `value` alongside the new
# visibility/ttl. The typed PUT (update_knowledge_page) would send a bare
# {"visibility": ...} body, which the server's UpdateKnowledgeRequest validator
# rejects with a 422 ("The value field is required"). When a typed content flag
# is also present, visibility/ttl ride along on the typed PUT instead.


@pytest.mark.asyncio
async def test_update_visibility_only_routes_to_legacy_page(monkeypatch):
    """`update ID --visibility private` (no other flag) must route through the
    legacy read-modify-write path (get_knowledge()+update_knowledge()) and
    forward visibility='private'. It must NOT call update_knowledge_page,
    because a bare {"visibility": ...} PUT body fails the server validator."""
    mod = _load_script()

    existing_entry = {"id": "01ABC", "value": {"content": "Old content"}}
    mock_get = AsyncMock(return_value=existing_entry)
    mock_update_legacy = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_page = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--visibility", "private",
        ])
        args.sort = None
        await mod._run(args)

    mock_update_page.assert_not_called()
    mock_update_legacy.assert_awaited_once()
    call = mock_update_legacy.call_args
    assert call.args[0] == "01ABC"
    assert call.kwargs["visibility"] == "private"
    # The legacy path re-sends a value (read-modify-write) so the validator is
    # satisfied — never a bare metadata body.
    assert "value" in call.kwargs


@pytest.mark.asyncio
async def test_update_ttl_only_routes_to_legacy_page(monkeypatch):
    """`update ID --ttl <iso>` (no other flag) must route through the legacy
    read-modify-write path and forward the ttl, NOT call update_knowledge_page."""
    mod = _load_script()

    existing_entry = {"id": "01ABC", "value": {"content": "Old content"}}
    mock_get = AsyncMock(return_value=existing_entry)
    mock_update_legacy = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_page = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--ttl", "2026-07-01T00:00:00Z",
        ])
        args.sort = None
        await mod._run(args)

    mock_update_page.assert_not_called()
    mock_update_legacy.assert_awaited_once()
    call = mock_update_legacy.call_args
    assert call.args[0] == "01ABC"
    assert call.kwargs["ttl"] == "2026-07-01T00:00:00Z"
    assert "value" in call.kwargs


@pytest.mark.asyncio
async def test_update_visibility_with_body_routes_to_typed_page(monkeypatch):
    """`update ID --body ... --visibility private` carries a typed content shape,
    so it routes to the typed PUT and visibility rides along on it."""
    mod = _load_script()

    mock_update_page = AsyncMock(return_value={"id": "01ABC"})
    mock_update_legacy = AsyncMock()
    mock_get = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--body", "New body", "--visibility", "private",
        ])
        args.sort = None
        await mod._run(args)

    mock_update_page.assert_awaited_once()
    call = mock_update_page.call_args
    assert call.args[0] == "01ABC"
    assert call.kwargs["body"] == "New body"
    assert call.kwargs["visibility"] == "private"
    # Typed content shape present, so no legacy read-modify-write.
    mock_update_legacy.assert_not_called()
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_update_visibility_only_outbound_body_carries_writable_shape(monkeypatch):
    """Contract-level guard: drive `update ID --visibility private` end-to-end
    through the CLI dispatcher against a real httpx transport and assert the
    captured PUT body carries a writable shape (`value`) — NOT a bare
    {"visibility": "private"} body, which the server's UpdateKnowledgeRequest
    validator rejects with a 422. This is the regression that mocking the client
    methods could not catch."""
    mod = _load_script()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://test.example")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive-x")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = {"data": {}, "meta": {}, "errors": []}
        if request.method == "GET":
            body["data"] = {
                "id": "01ABC",
                "value": {"content": "Existing content"},
            }
        return httpx.Response(200, json=body)

    real_init = mod.SuperposClient.__init__

    def patched_init(self, config):
        real_init(self, config)
        self._client = httpx.AsyncClient(
            base_url="https://test.example",
            transport=httpx.MockTransport(handler),
        )

    with patch.object(mod.SuperposClient, "__init__", patched_init):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--visibility", "private",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    put_requests = [r for r in captured if r.method == "PUT"]
    assert len(put_requests) == 1
    sent = json.loads(put_requests[0].content)
    # The outbound body must contain a writable shape, not bare metadata.
    assert "value" in sent, f"PUT body lacks a writable `value` shape: {sent}"
    assert sent["visibility"] == "private"
    # Existing content survives the read-modify-write.
    assert sent["value"].get("content") == "Existing content"


@pytest.mark.asyncio
async def test_update_visibility_with_content_stays_legacy(monkeypatch):
    """A legacy-only flag (--content) still pins the legacy path even when
    --visibility is also present, and the legacy path forwards visibility as
    it did before — back-compat for callers that combine the two."""
    mod = _load_script()

    existing_entry = {"id": "01ABC", "value": {"content": "Old content"}}
    mock_get = AsyncMock(return_value=existing_entry)
    mock_update_legacy = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_page = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--content", "Rule: new", "--visibility", "private",
        ])
        args.sort = None
        await mod._run(args)

    mock_update_legacy.assert_awaited_once()
    mock_update_page.assert_not_called()
    assert mock_update_legacy.call_args.kwargs["visibility"] == "private"


def _source_ids_help(subparser) -> str:
    """Return the help string of the --source-ids action on a subparser."""
    for action in subparser._actions:
        if "--source-ids" in action.option_strings:
            return action.help
    raise AssertionError("--source-ids action not found on subparser")


def test_source_ids_help_does_not_claim_auto_stamp():
    """The --source-ids help must not imply auto-stamping from SUPERPOS_AGENT_ID;
    source_ids is only forwarded when the user explicitly passes the flag."""
    mod = _load_script()
    parser = mod._build_parser()

    # Locate the create/update subparsers (the ones carrying --source-ids).
    subparsers_action = next(
        a for a in parser._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    checked = []
    for name in ("create", "update"):
        sub = subparsers_action.choices[name]
        help_text = _source_ids_help(sub)
        assert "Defaults to SUPERPOS_AGENT_ID" not in help_text, (
            f"{name} --source-ids help still claims a SUPERPOS_AGENT_ID default"
        )
        assert "not auto-stamped" in help_text, (
            f"{name} --source-ids help should state it is not auto-stamped"
        )
        checked.append(name)

    assert checked == ["create", "update"]


# --- presence-vs-truthiness parsing for --tags / --source-ids -----------------
# The backend distinguishes an *omitted* field from an explicit empty array:
# `tags: []` clears tags and `source_ids: []` removes citations. The CLI must
# therefore parse comma-list flags by PRESENCE, not truthiness — an explicit
# `--tags ""` sends `[]`, while an absent flag omits the field entirely.


def test_parse_csv_arg_distinguishes_absent_from_empty():
    """_parse_csv_arg: None -> None (omit), '' -> [] (explicit clear)."""
    mod = _load_script()
    assert mod._parse_csv_arg(None) is None
    assert mod._parse_csv_arg("") == []
    assert mod._parse_csv_arg("   ") == []  # whitespace-only also clears
    assert mod._parse_csv_arg("a, b ,c") == ["a", "b", "c"]
    assert mod._parse_csv_arg(",, ,") == []  # all-empty parts stripped


@pytest.mark.asyncio
async def test_update_typed_empty_tags_clears_tags(monkeypatch):
    """`update ID --tags ""` must send tags=[] (explicit clear) and exit 0,
    not exit 2."""
    mod = _load_script()

    mock_update_page = AsyncMock(return_value={"id": "01ABC", "tags": []})
    mock_update_legacy = AsyncMock()
    mock_get = AsyncMock()
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_legacy), \
         patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["update", "01ABC", "--tags", ""])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_update_page.assert_awaited_once()
    assert mock_update_page.call_args.kwargs["tags"] == []
    mock_update_legacy.assert_not_called()
    mock_get.assert_not_called()  # typed path, no read-modify-write


@pytest.mark.asyncio
async def test_update_typed_body_plus_empty_source_ids_clears_citations(monkeypatch):
    """`update ID --body new --source-ids ""` must send BOTH body AND
    source_ids=[] (clearing citations), not drop source_ids."""
    mod = _load_script()

    mock_update_page = AsyncMock(return_value={"id": "01ABC"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--body", "new", "--source-ids", "",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    kwargs = mock_update_page.call_args.kwargs
    assert kwargs["body"] == "new"
    assert kwargs["source_ids"] == []


@pytest.mark.asyncio
async def test_update_typed_absent_tags_omits_field(monkeypatch):
    """An absent --tags must omit tags entirely (no regression): the field is
    dropped, not sent as []."""
    mod = _load_script()

    mock_update_page = AsyncMock(return_value={"id": "01ABC"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "update_knowledge_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["update", "01ABC", "--body", "new"])
        args.sort = None
        await mod._run(args)

    kwargs = mock_update_page.call_args.kwargs
    # Absent flags are dropped before the client call.
    assert "tags" not in kwargs
    assert "source_ids" not in kwargs


@pytest.mark.asyncio
async def test_create_typed_empty_tags_and_source_ids_send_empty_lists(monkeypatch):
    """create --type ... --tags "" --source-ids "" must forward tags=[] and
    source_ids=[] (explicit empties), while absent flags would be None."""
    mod = _load_script()

    mock_create_page = AsyncMock(return_value={"id": "01NEW"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "s", "--body", "b",
            "--tags", "", "--source-ids", "",
        ])
        args.sort = None
        await mod._run(args)

    kwargs = mock_create_page.call_args.kwargs
    assert kwargs["tags"] == []
    assert kwargs["source_ids"] == []


@pytest.mark.asyncio
async def test_create_typed_absent_tags_and_source_ids_are_none(monkeypatch):
    """create --type ... with no --tags/--source-ids must pass None for both so
    the client omits them from the payload."""
    mod = _load_script()

    mock_create_page = AsyncMock(return_value={"id": "01NEW"})
    mock_close = AsyncMock()

    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(mod.SuperposClient, "create_knowledge_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "s", "--body", "b",
        ])
        args.sort = None
        await mod._run(args)

    kwargs = mock_create_page.call_args.kwargs
    assert kwargs["tags"] is None
    assert kwargs["source_ids"] is None
