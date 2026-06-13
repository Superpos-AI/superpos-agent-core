"""Tests for the ``superpos-task list`` and ``superpos-task show`` subcommands.

Mirrors ``test_superpos_task_self_target.py``: patches ``httpx.Client`` and
``_base_config`` to capture the outbound GET (URL + query params) and the
printed output, without touching the network.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from superpos_agent_core import superpos_task


def _run(cli_args, response_body, status_code=200):
    """Invoke the CLI with ``cli_args`` and capture the GET call + stdout."""
    captured: dict = {}

    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.text = ""

        def json(self):
            return response_body

    def fake_get(url, *, params=None, headers=None, **kwargs):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp()

    printed: list[str] = []

    with patch.object(superpos_task.httpx, "Client") as MockClient:
        MockClient.return_value.__enter__.return_value.get = fake_get
        with patch.object(
            superpos_task,
            "_base_config",
            return_value=("https://superpos.io", "hive-x", "agent-self", "tok"),
        ):
            with patch("sys.argv", ["superpos_task", *cli_args]):
                with patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
                    superpos_task.main()

    captured["stdout"] = "\n".join(printed)
    return captured


# ── list ──────────────────────────────────────────────────────────────────


def test_list_no_filters_sends_no_params():
    captured = _run(
        ["list"],
        {"data": [{"id": "t1", "type": "default", "status": "queued"}]},
    )
    assert captured["url"] == "/api/v1/hives/hive-x/tasks"
    assert captured["params"] is None
    assert "t1" in captured["stdout"]


def test_list_filters_become_query_params():
    captured = _run(
        [
            "list",
            "--status", "completed",
            "--type", "default",
            "--target-agent-id", "agent-7",
            "--target-capability", "coding",
            "--creator-id", "c1",
            "--parent-task-id", "p9",
            "--created-after", "2026-01-01T00:00:00Z",
            "--created-before", "2026-12-31T00:00:00Z",
            "--q", "needle",
            "--page", "2",
            "--per-page", "50",
        ],
        {"data": []},
    )
    assert captured["params"] == {
        "status": "completed",
        "type": "default",
        "target_agent_id": "agent-7",
        "target_capability": "coding",
        "creator_id": "c1",
        "parent_task_id": "p9",
        "created_after": "2026-01-01T00:00:00Z",
        "created_before": "2026-12-31T00:00:00Z",
        "q": "needle",
        "page": 2,
        "per_page": 50,
    }


def test_list_partial_filters_omit_unset():
    captured = _run(["list", "--status", "failed"], {"data": []})
    assert captured["params"] == {"status": "failed"}


def test_list_json_outputs_raw_data_list():
    body = {"data": [{"id": "t1", "type": "default", "status": "queued"}]}
    captured = _run(["list", "--json"], body)
    parsed = json.loads(captured["stdout"])
    assert parsed == body["data"]


def test_list_empty_prints_no_tasks():
    captured = _run(["list"], {"data": []})
    assert "No tasks found." in captured["stdout"]


# ── show ────────────────────────────────────────────────────────────────────


def test_show_hits_single_task_endpoint():
    captured = _run(
        ["show", "task-123"],
        {"data": {"id": "task-123", "type": "default", "status": "completed"}},
    )
    assert captured["url"] == "/api/v1/hives/hive-x/tasks/task-123"
    assert "task-123" in captured["stdout"]
    assert "completed" in captured["stdout"]


def test_show_json_outputs_raw_task():
    body = {"data": {"id": "task-123", "type": "default", "status": "completed"}}
    captured = _run(["show", "task-123", "--json"], body)
    assert json.loads(captured["stdout"]) == body["data"]
