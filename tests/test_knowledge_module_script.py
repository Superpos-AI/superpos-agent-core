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
async def test_typed_update_slug_no_match_errors(monkeypatch):
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
        with pytest.raises(SystemExit):
            await mod._run(args)
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
