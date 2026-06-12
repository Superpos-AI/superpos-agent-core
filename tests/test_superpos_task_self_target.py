"""Tests for the default targeting of `superpos_task.create`.

The default used to be `self_target=True`, which pinned every newly
created task back to the calling agent — a footgun in a multi-agent
hive where the whole point of the task queue is to route work to the
right worker. This file pins down the new default (`self_target=False`,
i.e. broadcast) and the positive opt-in.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from superpos_agent_core import superpos_task


def _run_create(*cli_args: str) -> dict:
    """Invoke the CLI's `create` subcommand and capture the POST body."""
    captured: dict = {}

    class _Resp:
        status_code = 201
        text = ""

        def json(self):
            return {"id": "t1"}

    def fake_post(url, *, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return _Resp()

    with patch.object(superpos_task.httpx, "Client") as MockClient:
        MockClient.return_value.__enter__.return_value.post = fake_post
        with patch.object(
            superpos_task,
            "_base_config",
            return_value=("https://superpos.io", "hive-x", "agent-self", "tok"),
        ):
            with patch("sys.argv", ["superpos_task", "create", *cli_args]):
                superpos_task.main()

    return captured


def test_create_broadcasts_by_default():
    """Default must be broadcast (no `target_agent_id`), not self-target."""
    captured = _run_create("--prompt", "do the thing")
    assert "target_agent_id" not in captured["body"]


def test_create_self_target_flag_pins_to_caller():
    """`--self-target` is the explicit opt-in to pin the task to the caller."""
    captured = _run_create("--prompt", "do the thing", "--self-target")
    assert captured["body"]["target_agent_id"] == "agent-self"


def test_create_capability_routes_by_capability():
    """When --capability is set, the task is targeted by capability, not by self."""
    captured = _run_create(
        "--prompt", "do the thing", "--capability", "knowledge-curator",
    )
    assert "target_agent_id" not in captured["body"]
    assert captured["body"]["target_capability"] == "knowledge-curator"


def test_create_task_function_default_is_broadcast():
    """`create_task(self_target=...)` kwarg default must be False (broadcast)."""
    import inspect
    sig = inspect.signature(superpos_task.create_task)
    assert sig.parameters["self_target"].default is False, (
        "Default for create_task(self_target=...) regressed to True — "
        "every agent in a multi-agent hive will stop receiving cross-agent work."
    )


def test_create_schedule_broadcasts_by_default():
    """`create_schedule` must also default to broadcast."""
    import inspect
    sig = inspect.signature(superpos_task.create_schedule)
    assert sig.parameters["self_target"].default is False
