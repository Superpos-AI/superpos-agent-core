"""Tests for the `superpos-task update` CLI subcommand (issue #97).

The helper module is imported directly (it is a real module, not a
PATH-only script), and ``httpx.Client`` is patched so we capture the
PATCH body/headers/url without a network round-trip — same approach as
``test_superpos_task_self_target.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from superpos_agent_core import superpos_task


def _run_update(*cli_args: str, status: int = 200):
    """Invoke the CLI's `update` subcommand; capture the PATCH request."""
    captured: dict = {}

    class _Resp:
        status_code = status
        text = '{"errors":[]}'

        def json(self):
            return {"data": {"id": "01HX", "ok": True}}

    def fake_patch(url, *, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return _Resp()

    with patch.object(superpos_task.httpx, "Client") as MockClient:
        MockClient.return_value.__enter__.return_value.patch = fake_patch
        with patch.object(
            superpos_task,
            "_base_config",
            return_value=("https://superpos.io", "hive-x", "agent-self", "tok"),
        ):
            with patch("sys.argv", ["superpos_task", "update", *cli_args]):
                superpos_task.main()

    return captured


# ── only-passed flags are sent ─────────────────────────────────────────


def test_only_passed_flags_are_sent():
    captured = _run_update("01HX", "--priority", "4", "--audit-reason", "bump")
    assert captured["body"] == {"priority": 4}
    assert captured["url"].endswith("/tasks/01HX")
    assert captured["headers"]["X-Audit-Reason"] == "bump"


def test_unpassed_flags_omitted():
    captured = _run_update("01HX", "--timeout-seconds", "60", "--audit-reason", "x")
    assert captured["body"] == {"timeout_seconds": 60}
    assert "target_agent_id" not in captured["body"]
    assert "priority" not in captured["body"]


# ── explicit null vs not-passed ────────────────────────────────────────


def test_empty_target_agent_id_sends_explicit_null():
    captured = _run_update(
        "01HX", "--target-agent-id", "", "--audit-reason", "redirect",
    )
    assert captured["body"] == {"target_agent_id": None}


def test_target_capability_and_broadcast():
    captured = _run_update(
        "01HX",
        "--target-capability", "data-analysis",
        "--target-agent-id", "",
        "--audit-reason", "broadcast to capable agent",
    )
    assert captured["body"] == {
        "target_agent_id": None,
        "target_capability": "data-analysis",
    }


def test_empty_expires_at_sends_null():
    captured = _run_update("01HX", "--expires-at", "", "--audit-reason", "clear ttl")
    assert captured["body"] == {"expires_at": None}


def test_nonempty_target_agent_id_sends_string():
    captured = _run_update(
        "01HX", "--target-agent-id", "agent-9", "--audit-reason", "pin",
    )
    assert captured["body"] == {"target_agent_id": "agent-9"}


# ── JSON parsing ───────────────────────────────────────────────────────


def test_payload_json_parsed_to_object():
    captured = _run_update(
        "01HX", "--payload", '{"k": 1, "stale": null}', "--audit-reason", "merge",
    )
    assert captured["body"] == {"payload": {"k": 1, "stale": None}}


def test_failure_policy_json_parsed_to_object():
    captured = _run_update(
        "01HX", "--failure-policy", '{"retry": false}', "--audit-reason", "policy",
    )
    assert captured["body"] == {"failure_policy": {"retry": False}}


def test_bad_json_errors_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        _run_update("01HX", "--payload", "{not json", "--audit-reason", "x")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--payload is not valid JSON" in err


def test_json_array_rejected(capsys):
    with pytest.raises(SystemExit) as exc:
        _run_update("01HX", "--payload", "[1,2,3]", "--audit-reason", "x")
    assert exc.value.code == 1
    assert "must be a JSON object" in capsys.readouterr().err


# ── audit reason warning ───────────────────────────────────────────────


def test_missing_audit_reason_warns_but_runs(capsys):
    captured = _run_update("01HX", "--priority", "2")
    # request still issued
    assert captured["body"] == {"priority": 2}
    # warning on stderr, no audit header
    assert "no --audit-reason given" in capsys.readouterr().err
    assert "X-Audit-Reason" not in captured["headers"]


# ── no actionable flags ────────────────────────────────────────────────


def test_no_fields_errors(capsys):
    with pytest.raises(SystemExit) as exc:
        _run_update("01HX", "--audit-reason", "nothing to change")
    assert exc.value.code == 1
    assert "no fields to update" in capsys.readouterr().err


# ── error status mapping ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,needle",
    [(422, "invalid"), (409, "terminal state"), (429, "rate limited")],
)
def test_error_statuses_mapped(status, needle, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_update("01HX", "--priority", "4", "--audit-reason", "x", status=status)
    assert exc.value.code == 1
    assert needle in capsys.readouterr().err
