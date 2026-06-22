"""Tests for superpos_task.update_memory write wrapping (AG-10, PR #53).

The CLI memory write is wrapped with ``persona_overlay.write_memory`` so a
genuine Superpos transport outage fails *loudly* via ``MemoryWriteUnavailable``
(no local fallback), while the existing status-code branches (which call
``sys.exit`` — a ``BaseException``) keep exiting cleanly.

httpx is monkeypatched so no network round-trip happens — same approach as
``test_superpos_task_self_target.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from superpos_agent_core import superpos_task


def _run_update_memory(monkeypatch, *, patch_behaviour, status: int = 200):
    """Invoke the `memory` subcommand with a stubbed httpx client.

    ``patch_behaviour`` is a callable used as the client's ``patch`` method.
    """
    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://superpos.io")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")

    with patch.object(superpos_task.httpx, "Client") as MockClient:
        MockClient.return_value.__enter__.return_value.patch = patch_behaviour
        with patch("sys.argv", ["superpos_task", "memory", "--content", "x"]):
            superpos_task.main()


def test_transport_error_fails_loudly(monkeypatch, capsys):
    """A genuine connection error → exit 1 via the MemoryWriteUnavailable path."""

    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(SystemExit) as exc:
        _run_update_memory(monkeypatch, patch_behaviour=boom)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "MEMORY write unavailable" in err


def test_locked_status_still_exits_cleanly(monkeypatch, capsys):
    """A 403 lock response keeps its existing message (sys.exit not swallowed)."""

    class _Resp:
        status_code = 403
        text = "locked"

        def json(self):
            return {}

    def patched(*args, **kwargs):
        return _Resp()

    with pytest.raises(SystemExit) as exc:
        _run_update_memory(monkeypatch, patch_behaviour=patched)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "locked by persona lock policy" in err
    # Must NOT be reported as a transport outage.
    assert "MEMORY write unavailable" not in err


def test_success_prints_updated(monkeypatch, capsys):
    """A 200 response prints the success message and does not exit."""

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {}

    def patched(*args, **kwargs):
        return _Resp()

    _run_update_memory(monkeypatch, patch_behaviour=patched)
    assert "Memory updated." in capsys.readouterr().out
