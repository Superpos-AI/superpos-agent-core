"""Tests for knowledge retrieval injection in superpos_poller.

Covers the pure ``_format_knowledge_block`` renderer and the best-effort
``_inject_knowledge`` helper (enabled/disabled, empty query, empty results,
and the must-never-raise failure path).
"""

from __future__ import annotations

import asyncio

from superpos_agent_core import superpos_poller as poller


# ── _format_knowledge_block (pure) ──────────────────────────────────────


def test_format_block_empty_returns_empty_string():
    assert poller._format_knowledge_block([]) == ""


def test_format_block_renders_key_id_and_gist():
    block = poller._format_knowledge_block([
        {"id": "01ABC", "key": "decisions:x", "value": {"summary": "we chose X"}},
    ])
    assert "## Relevant knowledge from the hive" in block
    assert "`decisions:x`" in block
    assert "(id `01ABC`)" in block
    assert "we chose X" in block


def test_format_block_gist_precedence_snippet_then_summary_then_title():
    # snippet (FTS) wins over value.summary
    snippet = poller._format_knowledge_block([
        {"key": "k", "snippet": "snip", "value": {"summary": "sum"}},
    ])
    assert "snip" in snippet and "sum" not in snippet
    # falls back to summary, then title
    summary = poller._format_knowledge_block([{"key": "k", "value": {"summary": "sum"}}])
    assert "sum" in summary
    title = poller._format_knowledge_block([{"key": "k", "value": {"title": "tit"}}])
    assert "tit" in title


def test_format_block_truncates_long_gist_and_collapses_whitespace():
    long_gist = "word " * 100
    block = poller._format_knowledge_block([{"key": "k", "snippet": long_gist}])
    # Collapsed (no double spaces) and truncated with an ellipsis.
    assert "  " not in block.split("\n\n")[-1]
    assert "…" in block


def test_format_block_skips_non_dict_entries():
    block = poller._format_knowledge_block(["nope", {"key": "ok"}])
    assert "`ok`" in block
    assert "nope" not in block


# ── _inject_knowledge (best-effort async) ───────────────────────────────


class _Cfg:
    superpos_knowledge_inject = True
    superpos_knowledge_inject_limit = 5


class _FakeClient:
    def __init__(self, result=None, raises=False):
        self._result = result if result is not None else []
        self._raises = raises
        self.calls: list[dict] = []

    async def search_knowledge(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError("boom")
        return self._result


def _inject(client, cfg, query, prompt):
    return asyncio.run(
        poller._inject_knowledge(client, cfg, query, prompt, "tsk_1")
    )


def test_inject_prepends_block_and_passes_semantic_and_limit():
    client = _FakeClient(result=[{"key": "decisions:x", "value": {"summary": "chose X"}}])
    out = _inject(client, _Cfg(), "do the auth migration", "ORIGINAL PROMPT")
    assert out.endswith("---\n\nORIGINAL PROMPT")
    assert "Relevant knowledge from the hive" in out
    assert client.calls[0]["semantic"] is True
    assert client.calls[0]["limit"] == 5
    assert client.calls[0]["q"] == "do the auth migration"


def test_inject_disabled_returns_prompt_untouched():
    cfg = _Cfg()
    cfg.superpos_knowledge_inject = False
    client = _FakeClient(result=[{"key": "k"}])
    out = _inject(client, cfg, "query", "P")
    assert out == "P"
    assert client.calls == []  # never even searched


def test_inject_blank_query_skips_search():
    client = _FakeClient(result=[{"key": "k"}])
    out = _inject(client, _Cfg(), "   ", "P")
    assert out == "P"
    assert client.calls == []


def test_inject_empty_results_returns_prompt_untouched():
    client = _FakeClient(result=[])
    out = _inject(client, _Cfg(), "query", "P")
    assert out == "P"


def test_inject_swallows_search_errors():
    client = _FakeClient(raises=True)
    out = _inject(client, _Cfg(), "query", "P")
    assert out == "P"  # error logged, dispatch continues


def test_inject_truncates_query_to_500_chars():
    client = _FakeClient(result=[])
    _inject(client, _Cfg(), "x" * 1000, "P")
    assert len(client.calls[0]["q"]) == 500
