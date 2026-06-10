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


# ── Typed-page read subcommands (AG-2) ──────────────────────────────────────


def _env_for_run(monkeypatch):
    """Common env setup for run-path tests in this section."""
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")


@pytest.mark.asyncio
async def test_get_by_slug_routes_to_get_knowledge_by_slug(monkeypatch):
    """`get-by-slug <slug>` must call get_knowledge_by_slug with the slug."""
    mod = _load_script()
    _env_for_run(monkeypatch)
    mock = AsyncMock(return_value={"id": "01ABC", "slug": "proposal-x", "type": "topic"})
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "get_knowledge_by_slug", mock), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["get-by-slug", "proposal-x"])
        args.sort = None
        await mod._run(args)
    mock.assert_awaited_once_with("proposal-x")


def test_parser_get_by_slug_requires_slug_arg():
    """`get-by-slug` without a positional slug must exit 2."""
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["get-by-slug"])
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_list_by_type_routes_to_list_knowledge_by_type(monkeypatch):
    """`list-by-type <type> --limit N` must forward both type and limit."""
    mod = _load_script()
    _env_for_run(monkeypatch)
    mock = AsyncMock(return_value=[{"id": "01ABC", "type": "topic"}])
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "list_knowledge_by_type", mock), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["list-by-type", "topic", "--limit", "7"])
        args.sort = None
        await mod._run(args)
    mock.assert_awaited_once_with("topic", limit=7)


def test_parser_list_by_type_requires_type_arg():
    """`list-by-type` without a positional type must exit 2."""
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["list-by-type"])
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_list_by_type_rejects_unknown_type(monkeypatch):
    """Unknown type values are rejected at parse/run time, before any network call."""
    mod = _load_script()
    _env_for_run(monkeypatch)
    mock = AsyncMock()
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "list_knowledge_by_type", mock), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["list-by-type", "bogus"])
        args.sort = None
        rc = await mod._run(args)
    assert rc == 2
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_lint_state_routes_to_get_knowledge_lint_state(monkeypatch):
    """`lint-state` (no args) must call get_knowledge_lint_state()."""
    mod = _load_script()
    _env_for_run(monkeypatch)
    mock = AsyncMock(return_value={"total": 0, "by_type": {}, "samples": []})
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "get_knowledge_lint_state", mock), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["lint-state"])
        args.sort = None
        await mod._run(args)
    mock.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_broken_links_routes_to_list_knowledge_broken_links(monkeypatch):
    """`broken-links` (no args) must call list_knowledge_broken_links()."""
    mod = _load_script()
    _env_for_run(monkeypatch)
    mock = AsyncMock(return_value=[])
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "list_knowledge_broken_links", mock), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["broken-links"])
        args.sort = None
        await mod._run(args)
    mock.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_backlinks_routes_to_list_knowledge_backlinks(monkeypatch):
    """`backlinks <slug>` must call list_knowledge_backlinks with the slug."""
    mod = _load_script()
    _env_for_run(monkeypatch)
    mock = AsyncMock(return_value=[{"slug": "proposal-x", "id": "01ABC"}])
    mock_close = AsyncMock()
    with patch.object(mod.SuperposClient, "list_knowledge_backlinks", mock), \
         patch.object(mod.SuperposClient, "close", mock_close):
        args = mod._build_parser().parse_args(["backlinks", "proposal-x"])
        args.sort = None
        await mod._run(args)
    mock.assert_awaited_once_with("proposal-x")


def test_parser_backlinks_requires_slug_arg():
    """`backlinks` without a positional slug must exit 2."""
    mod = _load_script()
    parser = mod._build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["backlinks"])
    assert exc.value.code == 2


