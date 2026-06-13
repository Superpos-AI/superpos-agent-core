"""setup_logging must tame httpx/httpcore's per-request INFO spam.

Each agent issues tens of thousands of HTTP calls a day; at INFO they bury
the WARNING/ERROR lines that actually matter (failed polls, CLI crashes).
"""

from __future__ import annotations

import logging

from superpos_agent_core.main import setup_logging


def test_httpx_and_httpcore_lifted_to_warning(tmp_path):
    # Start noisy so the test proves setup_logging is what quiets them.
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)

    setup_logging(str(tmp_path / "logs"))

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_agent_loggers_left_untouched(tmp_path):
    # Reset to inherit-from-root so the test proves setup_logging does NOT
    # pin an explicit level on the agent's own loggers.
    logging.getLogger("superpos_agent_core.superpos_poller").setLevel(logging.NOTSET)

    setup_logging(str(tmp_path / "logs"))

    # Only httpx/httpcore get an explicit WARNING; agent loggers stay at
    # NOTSET so they inherit the root (INFO in production) and keep emitting.
    assert logging.getLogger("superpos_agent_core.superpos_poller").level == logging.NOTSET
