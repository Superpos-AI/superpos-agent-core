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

import httpx
import pytest

from superpos_agent_core import bundled_modules_dir


def _set_env(monkeypatch):
    monkeypatch.setenv("SUPERPOS_BASE_URL", "http://fake")
    monkeypatch.setenv("SUPERPOS_HIVE_ID", "hive1")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

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
        # typed-shape attrs
        type=None, slug=None, body=None, body_file=None, frontmatter=None,
        source_ids=None,
        id=None, visibility=None, ttl=None, scope=None, key=None,
        entry_id=None,
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


def test_parser_create_accepts_legacy_and_typed_flags():
    mod = _load_script()
    parser = mod._build_parser()
    # Requiredness for key/content is enforced in the handler (so the typed
    # shape can omit them), not at the argparse level — so a bare `create`
    # now parses without error.
    args = parser.parse_args(["create"])
    assert args.cmd == "create" and args.key is None and args.content is None
    # legacy create parses
    args = parser.parse_args(["create", "--key", "k", "--content", "c"])
    assert args.cmd == "create" and args.key == "k" and args.content == "c"
    # typed create parses
    args = parser.parse_args([
        "create", "--type", "topic", "--slug", "topic:x", "--body", "b",
    ])
    assert args.type == "topic" and args.slug == "topic:x" and args.body == "b"


def test_parser_update_takes_entry_id_and_optional_content():
    mod = _load_script()
    parser = mod._build_parser()
    args = parser.parse_args(["update", "01ABC", "--summary", "s"])
    assert args.cmd == "update" and args.entry_id == "01ABC"
    assert args.content is None  # not required on update


@pytest.mark.asyncio
async def test_update_legacy_partial_flags_preserve_existing_fields(monkeypatch):
    """A *legacy* partial update (a positional id with a legacy flag like
    --content) must not drop existing fields — it reads, merges, and writes.

    (A positional id with a *shared content* flag like --summary now routes to
    the typed update_page path; see
    test_typed_update_positional_entry_id_summary_routes_to_update_page. To stay
    on the legacy read-modify-write path here we pair the positional id with a
    legacy flag, then assert the merged value still carries the new content and
    every preserved field.)
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
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--content", "Rule: new content",
        ])
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
async def test_update_value_legacy_only_flag_overrides_still_merge(monkeypatch):
    """When --value is combined with a *legacy* individual flag (e.g.
    --content), the read-modify-write merge path is still used.

    (Combining --value with a *shared content* flag like --summary also stays on
    this merge path — the legacy flag pins the legacy shape for shared fields;
    see test_update_value_with_shared_content_flag_routes_legacy. A pure legacy
    --value/--content combination stays on the merge path too.)
    """
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
            "--content", "new content",
        ])
        args.sort = None
        await mod._run(args)

    # get_knowledge IS called because individual flags trigger read-modify-write
    mock_get.assert_called_once()

    sent_value = mock_update.call_args.kwargs["value"]
    # --value provides title="base", --content overrides content
    assert sent_value["title"] == "base"
    assert sent_value["content"] == "new content"
    # Existing field "summary" is preserved via merge
    assert sent_value["summary"] == "old summary"
    # Provenance metadata stamped
    assert sent_value["metadata"]["source"] == "agent_inline"
    assert sent_value["metadata"]["auto_generated"] is True


@pytest.mark.asyncio
async def test_update_value_with_shared_content_flag_routes_legacy(
    monkeypatch,
):
    """``update <id> --value Y --summary X`` pairs the legacy shape (--value)
    with a *shared* content flag (--summary), which is valid legacy content. The
    legacy flag pins the legacy read-modify-write path — it must NOT be rejected
    as a typed/legacy mix, and the shared field is merged into the value.

    Regression: the positional id + shared content flag was wrongly promoted to
    typed even when a legacy flag was present, so the XOR guard rejected this
    valid legacy update (exit 2) before any API call.
    """
    mod = _load_script()
    _set_env(monkeypatch)
    mock_get = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_k = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_page = AsyncMock()
    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_k), \
         patch.object(mod.KnowledgeClient, "update_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC",
            "--value", '{"title": "base"}',
            "--summary", "new summary",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_update_k.assert_called_once()
    mock_update_page.assert_not_called()
    sent_value = mock_update_k.call_args.kwargs["value"]
    assert sent_value["title"] == "base"
    assert sent_value["summary"] == "new summary"


# ── pure helper tests (typed shape) ───────────────────────────────────


def test_resolve_body_reads_body_file(tmp_path):
    mod = _load_script()
    f = tmp_path / "page.md"
    f.write_text("# Hello\n\nbody", encoding="utf-8")
    assert mod._resolve_body(_ns(body_file=str(f))) == "# Hello\n\nbody"
    assert mod._resolve_body(_ns(body="inline")) == "inline"
    assert mod._resolve_body(_ns()) is None


def test_resolve_body_rejects_both(capsys):
    mod = _load_script()
    with pytest.raises(SystemExit):
        mod._resolve_body(_ns(body="x", body_file="y"))
    assert "mutually exclusive" in capsys.readouterr().err


def test_parse_json_arg_bad_json_exits(capsys):
    mod = _load_script()
    with pytest.raises(SystemExit):
        mod._parse_json_arg("frontmatter", "{not json")
    assert "must be valid JSON" in capsys.readouterr().err


def test_guard_shape_xor_rejects_mixed(capsys):
    mod = _load_script()
    with pytest.raises(SystemExit):
        mod._guard_shape_xor(_ns(type="topic", key="k"))
    assert "cannot mix" in capsys.readouterr().err
    # pure typed → True, pure legacy → False
    assert mod._guard_shape_xor(_ns(type="topic", slug="topic:x", body="b")) is True
    assert mod._guard_shape_xor(_ns(key="k", content="c")) is False


# ── routing tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_typed_create_routes_to_create_page(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    mock_create = AsyncMock(return_value={"id": "kxe_1", "type": "topic"})
    with patch.object(mod.KnowledgeClient, "create_page", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "topic:x",
            "--body", "# body", "--summary", "s", "--title", "T",
            "--tags", "a, b", "--frontmatter", '{"status": "ok"}',
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    kw = mock_create.call_args.kwargs
    assert kw["type"] == "topic"
    assert kw["slug"] == "topic:x"
    assert kw["body"] == "# body"
    assert kw["summary"] == "s"
    assert kw["title"] == "T"
    assert kw["tags"] == ["a", "b"]
    assert kw["frontmatter"] == {"status": "ok"}


@pytest.mark.asyncio
async def test_typed_create_forwards_ttl(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    mock_create = AsyncMock(return_value={"id": "kxe_1", "type": "topic"})
    with patch.object(mod.KnowledgeClient, "create_page", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "topic:x",
            "--body", "# body", "--ttl", "2026-06-11T00:00:00Z",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    assert mock_create.call_args.kwargs["ttl"] == "2026-06-11T00:00:00Z"


@pytest.mark.asyncio
async def test_typed_create_forwards_source_ids(monkeypatch):
    """``--source-ids 01SRC1,01SRC2`` is parsed and forwarded verbatim to
    create_page (restores the main-branch typed source attach interface that
    the merge dropped)."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_create = AsyncMock(return_value={"id": "kxe_1", "type": "topic"})
    with patch.object(mod.KnowledgeClient, "create_page", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "topic:x",
            "--body", "# body", "--source-ids", "01SRC1,01SRC2",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    assert mock_create.call_args.kwargs["source_ids"] == ["01SRC1", "01SRC2"]


@pytest.mark.asyncio
async def test_typed_create_absent_source_ids_is_none(monkeypatch):
    """No ``--source-ids`` → source_ids=None (omitted from the payload, not
    auto-stamped from SUPERPOS_AGENT_ID — auto-stamp would 403 per §6.8)."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_create = AsyncMock(return_value={"id": "kxe_1"})
    with patch.object(mod.KnowledgeClient, "create_page", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "topic:x", "--body", "# body",
        ])
        args.sort = None
        await mod._run(args)
    assert mock_create.call_args.kwargs["source_ids"] is None


@pytest.mark.asyncio
async def test_typed_update_forwards_source_ids(monkeypatch):
    """``update --id X --source-ids 01SRC`` forwards source_ids to update_page
    (source_ids is itself a content field, so no cross-cutting guard fires)."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock(return_value={"id": "kxe_1", "version": 2})
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1", "--source-ids", "01SRC",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    assert mock_update.call_args.kwargs["source_ids"] == ["01SRC"]


@pytest.mark.asyncio
async def test_typed_update_empty_source_ids_clears_citations(monkeypatch):
    """``update --id X --source-ids ""`` sends source_ids=[] to clear citations
    (presence-vs-truthiness: an explicit empty value detaches all sources,
    while an absent flag would be None / omitted)."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock(return_value={"id": "kxe_1", "version": 2})
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1", "--source-ids", "",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    assert mock_update.call_args.kwargs["source_ids"] == []


def test_source_ids_is_a_typed_flag(capsys):
    """``--source-ids`` selects the typed path and must not be mixed with legacy
    flags (regression: the merge dropped the flag entirely, so a valid main
    command failed argparse with 'unrecognized arguments')."""
    mod = _load_script()
    assert mod._guard_shape_xor(_ns(source_ids="01SRC")) is True
    with pytest.raises(SystemExit):
        mod._guard_shape_xor(_ns(source_ids="01SRC", content="Rule: ..."))
    assert "cannot mix" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_typed_create_body_file(monkeypatch, tmp_path):
    mod = _load_script()
    _set_env(monkeypatch)
    f = tmp_path / "p.md"
    f.write_text("from file", encoding="utf-8")
    mock_create = AsyncMock(return_value={"id": "kxe_1"})
    with patch.object(mod.KnowledgeClient, "create_page", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "topic:x",
            "--body-file", str(f),
        ])
        args.sort = None
        await mod._run(args)
    assert mock_create.call_args.kwargs["body"] == "from file"


@pytest.mark.asyncio
async def test_typed_create_bad_frontmatter_json_errors(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    mock_create = AsyncMock()
    with patch.object(mod.KnowledgeClient, "create_page", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "topic:x",
            "--body", "b", "--frontmatter", "{bad",
        ])
        args.sort = None
        with pytest.raises(SystemExit):
            await mod._run(args)
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_typed_create_entity_requires_kind(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    mock_create = AsyncMock()
    with patch.object(mod.KnowledgeClient, "create_page", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--type", "entity", "--slug", "entity:x", "--body", "b",
        ])
        args.sort = None
        with pytest.raises(SystemExit):
            await mod._run(args)
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_create_routes_to_create_knowledge_with_deprecation(
    monkeypatch, capsys,
):
    mod = _load_script()
    _set_env(monkeypatch)
    mock_create = AsyncMock(return_value={"id": "01ABC"})
    mock_kpage = AsyncMock()
    with patch.object(mod.SuperposClient, "create_knowledge", mock_create), \
         patch.object(mod.KnowledgeClient, "create_page", mock_kpage), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--key", "decisions:x", "--content", "Rule: y",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_create.assert_called_once()
    mock_kpage.assert_not_called()
    assert "deprecated" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_create_mixing_typed_and_legacy_errors_no_call(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    mock_create_k = AsyncMock()
    mock_create_page = AsyncMock()
    with patch.object(mod.SuperposClient, "create_knowledge", mock_create_k), \
         patch.object(mod.KnowledgeClient, "create_page", mock_create_page), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "topic:x",
            "--body", "b", "--key", "k",
        ])
        args.sort = None
        with pytest.raises(SystemExit):
            await mod._run(args)
    mock_create_k.assert_not_called()
    mock_create_page.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_by_id_routes_to_update_page(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock(return_value={"id": "kxe_1", "version": 2})
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1", "--body", "new", "--summary", "s",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_list.assert_not_called()  # id given → no slug resolution
    assert mock_update.call_args.args[0] == "kxe_1"
    kw = mock_update.call_args.kwargs
    assert kw["body"] == "new"
    assert kw["summary"] == "s"


@pytest.mark.asyncio
async def test_typed_update_positional_entry_id_routes_to_update_page(monkeypatch):
    """``update <entry_id> --body ...`` (POSITIONAL id, no --id) must route to
    the typed update path and call update_page with that id — exit 0, no error.

    Backwards-compat regression: before --id existed, the positional entry_id
    was the only way to point a typed update at a page; introducing --id must
    not break the positional spelling.
    """
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock(return_value={"id": "01ABC", "version": 2})
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--body", "new body",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_list.assert_not_called()  # positional id given → no slug resolution
    assert mock_update.call_args.args[0] == "01ABC"
    assert mock_update.call_args.kwargs["body"] == "new body"


@pytest.mark.asyncio
async def test_typed_update_positional_entry_id_summary_routes_to_update_page(
    monkeypatch,
):
    """``update <id> --summary ...`` (POSITIONAL id, only a shared content flag,
    no legacy/typed-exclusive flag) must route to the TYPED update_page path —
    NOT the legacy update_knowledge path.

    Regression: ``--summary`` is a shared content flag and the positional
    entry_id is a separate argparse dest from ``--id``, so the routing guard
    saw neither a typed flag nor a typed selector and silently fell through to
    the legacy read-modify-write path. A positional-id update carrying a shared
    content field is an unambiguous typed edit.
    """
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock(return_value={"id": "01ABC", "version": 2})
    mock_update_k = AsyncMock()
    mock_get = AsyncMock()
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_k), \
         patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--summary", "New summary",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_update.assert_called_once()
    mock_update_k.assert_not_called()  # NOT the legacy path
    mock_get.assert_not_called()  # no legacy read-modify-write
    mock_list.assert_not_called()  # positional id given → no slug resolution
    assert mock_update.call_args.args[0] == "01ABC"
    assert mock_update.call_args.kwargs["summary"] == "New summary"


@pytest.mark.asyncio
async def test_typed_update_positional_entry_id_empty_tags_clears_via_update_page(
    monkeypatch,
):
    """``update <id> --tags ""`` (POSITIONAL id, empty tags) must route to the
    TYPED update_page path with tags cleared (an empty list), NOT the legacy
    update_knowledge path.

    An empty ``--tags`` is a deliberate "clear the tags" content change, so it
    counts as a content field and routes typed just like a non-empty value.
    """
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock(return_value={"id": "01ABC", "version": 2})
    mock_update_k = AsyncMock()
    mock_get = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_k), \
         patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--tags", "",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_update.assert_called_once()
    mock_update_k.assert_not_called()  # NOT the legacy path
    mock_get.assert_not_called()
    assert mock_update.call_args.args[0] == "01ABC"
    assert mock_update.call_args.kwargs["tags"] == []  # cleared, not None


@pytest.mark.asyncio
async def test_typed_update_positional_entry_id_with_slug_rejected(monkeypatch):
    """A positional entry_id and --type/--slug both identify the page; giving
    both is ambiguous and must fail fast before any API call (mirrors the
    --id selector guard)."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--type", "topic", "--slug", "topic:x",
            "--body", "new body",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update.assert_not_called()
    mock_list.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_positional_entry_id_and_id_mismatch_rejected(monkeypatch):
    """The positional entry_id is an alias for --id; if both are given but
    differ, refuse rather than silently picking one."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--id", "kxe_other", "--body", "new body",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_update_positional_entry_id_with_legacy_flags_routes_legacy(monkeypatch):
    """A positional entry_id with ONLY legacy flags (e.g. --content) is a
    plain legacy key/value update and must route to update_knowledge, never
    update_page."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_get = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_k = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_page = AsyncMock()
    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_k), \
         patch.object(mod.KnowledgeClient, "update_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--content", "Rule: new content",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_update_k.assert_called_once()
    mock_update_page.assert_not_called()


@pytest.mark.asyncio
async def test_update_positional_id_legacy_flag_with_title_routes_legacy(monkeypatch):
    """``update <id> --content "Rule: ..." --title "New title"`` is a legacy
    update with a shared content field, NOT a typed/legacy mix. It must route to
    update_knowledge (read-modify-write) and merge the title into the value —
    never raise the XOR error.

    Regression: a legacy flag alongside the positional id + a shared content
    flag was wrongly promoted to typed, so the XOR guard rejected a valid legacy
    update before either endpoint was called (exit 2). The legacy flag must pin
    the legacy path for shared fields.
    """
    mod = _load_script()
    _set_env(monkeypatch)
    mock_get = AsyncMock(return_value={"id": "01ABC", "value": {"title": "old"}})
    mock_update_k = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_page = AsyncMock()
    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_k), \
         patch.object(mod.KnowledgeClient, "update_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--content", "Rule: new content",
            "--title", "New title",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_update_k.assert_called_once()
    mock_update_page.assert_not_called()
    # The shared --title and legacy --content both land in the merged value.
    sent_value = mock_update_k.call_args.kwargs["value"]
    assert sent_value["title"] == "New title"
    assert sent_value["content"] == "Rule: new content"


@pytest.mark.asyncio
async def test_update_positional_id_legacy_flag_with_tags_routes_legacy(monkeypatch):
    """``update <id> --content ... --tags a,b`` (legacy flag + shared --tags)
    routes to update_knowledge with the tags merged into the value, not the XOR
    error path."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_get = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_k = AsyncMock(return_value={"id": "01ABC", "value": {}})
    mock_update_page = AsyncMock()
    with patch.object(mod.SuperposClient, "get_knowledge", mock_get), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_k), \
         patch.object(mod.KnowledgeClient, "update_page", mock_update_page), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--content", "Rule: c", "--tags", "a,b",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_update_k.assert_called_once()
    mock_update_page.assert_not_called()
    sent_value = mock_update_k.call_args.kwargs["value"]
    assert sent_value["tags"] == ["a", "b"]
    assert sent_value["content"] == "Rule: c"


@pytest.mark.asyncio
async def test_update_positional_entry_id_mixing_typed_and_legacy_rejected(monkeypatch):
    """``update <id> --body ... --content ...`` mixes typed and legacy shapes;
    the XOR guard must reject it before any API call (the positional id rides
    along with whichever shape, but the shapes themselves can't be mixed).

    Note: this is a genuinely typed-EXCLUSIVE flag (--body) mixed with legacy,
    which is still rejected — distinct from a shared content flag (--title/
    --summary/--tags) + legacy, which stays legacy (see the two tests above).
    """
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update_page = AsyncMock()
    mock_update_k = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update_page), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_k), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--body", "b", "--content", "Rule: c",
        ])
        args.sort = None
        with pytest.raises(SystemExit):
            await mod._run(args)
    mock_update_page.assert_not_called()
    mock_update_k.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_forwards_ttl_with_content(monkeypatch):
    """``--ttl`` is forwarded when it accompanies a real content field.

    (Regression: ttl alone routed a cross-cutting-only payload the server
    rejects with "value required"; see test_typed_update_ttl_only_rejected.)
    """
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock(return_value={"id": "kxe_1", "version": 2})
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1", "--summary", "s",
            "--ttl", "2026-06-11T00:00:00Z",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_list.assert_not_called()
    assert mock_update.call_args.args[0] == "kxe_1"
    assert mock_update.call_args.kwargs["ttl"] == "2026-06-11T00:00:00Z"
    assert mock_update.call_args.kwargs["summary"] == "s"


@pytest.mark.asyncio
async def test_typed_update_ttl_only_rejected(monkeypatch):
    """``update --id X --ttl <future>`` with NO content field must fail fast and
    never call update_page: a typed payload of only {"ttl": …} is rejected by
    the server's UpdateKnowledgeRequest (its writable-shape guard excludes the
    cross-cutting ttl/visibility fields, so "value" is required)."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1", "--ttl", "2026-06-11T00:00:00Z",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update.assert_not_called()
    mock_list.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_ttl_and_visibility_only_rejected(monkeypatch):
    """ttl + visibility together (still no content) is also cross-cutting-only
    and must be rejected before any API call."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1",
            "--ttl", "2026-06-11T00:00:00Z", "--visibility", "private",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_id_only_no_content_rejected(monkeypatch):
    """``update --id X`` with NO content and NO cross-cutting field must fail
    fast and never call update_page: --id alone routes to the typed path, and a
    content-less typed update would otherwise PUT an empty {} body — surfacing a
    server validation error / no-op as a successful (exit 0) command. The PR
    contract is that content-less typed updates are rejected client-side."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args(["update", "--id", "kxe_1"])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update.assert_not_called()
    mock_list.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_slug_only_no_content_rejected(monkeypatch):
    """``update --type topic --slug s`` with no content field must also be
    rejected before any API call (and before resolving the slug to an id) —
    same empty-{}-payload bug via the slug selector instead of --id."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--type", "topic", "--slug", "s",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_id_with_type_rejected(monkeypatch):
    """``update --id X --type entity`` is a re-type attempt: a page's type is
    immutable on update (re-typing is a migration), and update_page has no type
    param, so --type was silently ignored (exit 0). It must now fail fast and
    never call update_page — --type is only valid alongside --slug."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1", "--type", "entity", "--body", "new",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update.assert_not_called()
    mock_list.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_positional_entry_id_with_type_rejected(monkeypatch):
    """Same re-type rejection via the positional entry_id alias:
    ``update <id> --type entity --body new`` must fail fast with no API call."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "01ABC", "--type", "entity", "--body", "new",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update.assert_not_called()
    mock_list.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_type_without_id_or_slug_rejected(monkeypatch):
    """``update --type entity --body new`` with neither an id nor a --slug has
    no page to resolve and no slug to use --type for — reject before any API
    call rather than silently ignore --type."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--type", "entity", "--body", "new",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc:
            await mod._run(args)

    assert exc.value.code == 2
    mock_update.assert_not_called()
    mock_list.assert_not_called()


def test_guard_shape_xor_treats_id_as_typed_selector(capsys):
    """``--id`` selects the typed path, so it must not be mixed with legacy
    flags (regression: ``--id`` was previously not seen by the guard, letting
    ``update --id ... --content`` bypass the XOR and send an empty update)."""
    mod = _load_script()
    # --id alone (or with shared flags) → typed.
    assert mod._guard_shape_xor(_ns(id="kxe_1")) is True
    assert mod._guard_shape_xor(_ns(id="kxe_1", summary="s")) is True
    # --id mixed with a legacy flag → rejected, not silently typed.
    with pytest.raises(SystemExit):
        mod._guard_shape_xor(_ns(id="kxe_1", content="Rule: ..."))
    assert "cannot mix" in capsys.readouterr().err


def test_guard_shape_xor_positional_id_with_shared_content_is_typed(capsys):
    """A positional ``entry_id`` carrying a shared content flag
    (--title/--summary/--tags) AND no legacy flag selects the typed path, since
    the positional id aliases --id. Pure cross-cutting (--ttl/--visibility) or
    bare positional ids stay legacy; a shared content flag alongside a legacy
    flag stays legacy too (the legacy flag pins the legacy path).

    Regression: the positional entry_id is a separate argparse dest from --id,
    so a positional-id update with only a shared content flag used to read as
    neither typed nor legacy and fell through to the legacy path.
    """
    mod = _load_script()
    # positional id + shared content flag (no legacy flag) → typed.
    assert mod._guard_shape_xor(_ns(entry_id="01ABC", summary="s")) is True
    assert mod._guard_shape_xor(_ns(entry_id="01ABC", title="T")) is True
    assert mod._guard_shape_xor(_ns(entry_id="01ABC", tags="")) is True
    # bare positional id, or one with only cross-cutting/legacy fields → legacy.
    assert mod._guard_shape_xor(_ns(entry_id="01ABC")) is False
    assert mod._guard_shape_xor(_ns(entry_id="01ABC", ttl="2026-06-11")) is False
    assert mod._guard_shape_xor(_ns(entry_id="01ABC", value="{}")) is False
    # positional id + shared content flag + a legacy flag → legacy, NOT rejected.
    # The shared fields are valid legacy content, so the legacy flag pins the
    # legacy read-modify-write path (main-branch backwards compatibility).
    assert mod._guard_shape_xor(_ns(entry_id="01ABC", summary="s", value="{}")) is False
    assert mod._guard_shape_xor(_ns(entry_id="01ABC", title="T", content="Rule: c")) is False
    assert mod._guard_shape_xor(_ns(entry_id="01ABC", tags="a,b", content="Rule: c")) is False


@pytest.mark.asyncio
async def test_update_id_with_legacy_flags_errors_no_call(monkeypatch):
    """``update --id ... --content`` must fail fast and send nothing — neither a
    legacy update nor an empty typed update (regression for the routing bug)."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update_page = AsyncMock()
    mock_update_k = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update_page), \
         patch.object(mod.SuperposClient, "update_knowledge", mock_update_k), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1", "--content", "Rule: do the thing",
        ])
        args.sort = None
        with pytest.raises(SystemExit):
            await mod._run(args)
    mock_update_page.assert_not_called()
    mock_update_k.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_by_slug_resolves_then_updates(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    mock_list = AsyncMock(return_value=[
        {"id": "kxe_other", "slug": "topic:other"},
        {"id": "kxe_match", "slug": "proposal-knowledge-wiki"},
    ])
    mock_update = AsyncMock(return_value={"id": "kxe_match", "version": 5})
    with patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--type", "topic", "--slug", "proposal-knowledge-wiki",
            "--summary", "refreshed",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    mock_list.assert_called_once_with("topic", limit=100)
    assert mock_update.call_args.args[0] == "kxe_match"
    assert mock_update.call_args.kwargs["summary"] == "refreshed"


@pytest.mark.asyncio
async def test_typed_update_slug_without_type_errors(monkeypatch):
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--slug", "proposal-knowledge-wiki", "--summary", "x",
        ])
        args.sort = None
        with pytest.raises(SystemExit):
            await mod._run(args)
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_visibility_only_errors(monkeypatch):
    """``update --id ... --visibility private`` with no content field must fail
    fast and send nothing — the server rejects a visibility-only typed update
    (visibility isn't a typed/legacy/source update field)."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock()
    mock_list = AsyncMock()
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1", "--visibility", "private",
        ])
        args.sort = None
        with pytest.raises(SystemExit):
            await mod._run(args)
    mock_update.assert_not_called()
    mock_list.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_visibility_with_content_routes_to_update_page(monkeypatch):
    """``--visibility`` alongside a content field is fine — the guard only
    blocks the visibility-only case, so visibility is still forwarded."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_update = AsyncMock(return_value={"id": "kxe_1", "version": 3})
    with patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--id", "kxe_1", "--visibility", "private",
            "--body", "new body",
        ])
        args.sort = None
        rc = await mod._run(args)

    assert rc == 0
    assert mock_update.call_args.args[0] == "kxe_1"
    kw = mock_update.call_args.kwargs
    assert kw["visibility"] == "private"
    assert kw["body"] == "new body"


@pytest.mark.asyncio
async def test_typed_update_slug_no_match_under_cap_errors(monkeypatch, capsys):
    """Result set under the cap → the whole type was scanned, so the slug
    genuinely doesn't exist: report a plain "no page found" miss."""
    mod = _load_script()
    _set_env(monkeypatch)
    mock_list = AsyncMock(return_value=[{"id": "a", "slug": "topic:other"}])
    mock_update = AsyncMock()
    with patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--type", "topic", "--slug", "nope", "--summary", "x",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc_info:
            await mod._run(args)
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "no topic page found with slug 'nope'." in err
    assert "most recent" not in err
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_typed_update_slug_no_match_at_cap_discloses_bound(monkeypatch, capsys):
    """Result set at the cap → it was truncated to the newest entries, so the
    page may exist but is unreachable by slug: disclose the bound and point at
    --id."""
    mod = _load_script()
    _set_env(monkeypatch)
    # Exactly the cap's worth of pages, none matching the target slug.
    pages = [{"id": f"k{i}", "slug": f"topic:n{i}"} for i in range(100)]
    mock_list = AsyncMock(return_value=pages)
    mock_update = AsyncMock()
    with patch.object(mod.KnowledgeClient, "list_by_type", mock_list), \
         patch.object(mod.KnowledgeClient, "update_page", mock_update), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "update", "--type", "topic", "--slug", "nope", "--summary", "x",
        ])
        args.sort = None
        with pytest.raises(SystemExit) as exc_info:
            await mod._run(args)
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "100 most recent entries" in err
    assert "--id" in err
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_typed_create_422_propagates_server_message(monkeypatch, capsys):
    """A server 422 (bad shape / frontmatter) surfaces as an HTTPStatusError
    whose message reaches the user (asyncio.run re-raises out of main)."""
    mod = _load_script()
    _set_env(monkeypatch)

    request = httpx.Request("POST", "http://fake/knowledge")
    response = httpx.Response(
        422, request=request,
        json={"errors": [{"field": "frontmatter", "message": "kind required"}]},
    )
    err = httpx.HTTPStatusError("422", request=request, response=response)
    mock_create = AsyncMock(side_effect=err)
    with patch.object(mod.KnowledgeClient, "create_page", mock_create), \
         patch.object(mod.SuperposClient, "close", AsyncMock()):
        args = mod._build_parser().parse_args([
            "create", "--type", "topic", "--slug", "topic:x", "--body", "b",
        ])
        args.sort = None
        with pytest.raises(httpx.HTTPStatusError):
            await mod._run(args)
