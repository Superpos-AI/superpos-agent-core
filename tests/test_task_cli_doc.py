"""Tests for the auto-generated ``superpos-task`` CLI reference.

The whole point of :mod:`superpos_agent_core.task_cli_doc` is that the docs
*cannot* drift from the real flags. These tests encode that guarantee:

- every subcommand and every flag the parser defines must appear in the
  rendered Markdown (the anti-drift meta-test), and
- the rendered output reflects the *current* ``create`` semantics
  (``--self-target``, default broadcast) — locking in the inversion whose
  earlier silent drift motivated this module.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from superpos_agent_core.superpos_task import build_parser
from superpos_agent_core.task_cli_doc import render_task_cli_reference


def _subparsers(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def test_build_parser_round_trips_self_target_semantics():
    """Guard the build_parser() refactor: parsing still wires --self-target
    to a default-broadcast (self_target False unless the flag is passed)."""
    parser = build_parser()

    broadcast = parser.parse_args(["create", "--prompt", "hi"])
    assert broadcast.self_target is False  # default: broadcast

    pinned = parser.parse_args(["create", "--prompt", "hi", "--self-target"])
    assert pinned.self_target is True


def test_every_subcommand_appears_in_doc():
    parser = build_parser()
    doc = render_task_cli_reference(parser)
    for name in _subparsers(parser):
        assert f"`superpos-task {name}`" in doc, f"subcommand {name!r} missing from doc"


def test_every_flag_and_positional_appears_in_doc():
    """The anti-drift contract: nothing the parser accepts is undocumented.

    Walk every argument of every subparser and assert its primary token
    (option string or positional dest) shows up verbatim in the rendered
    reference. If someone adds a flag to build_parser() and this test fails,
    the doc generator — not a human — needs to keep up, which it does
    automatically; the test simply proves the wiring is intact."""
    parser = build_parser()
    doc = render_task_cli_reference(parser)

    for name, subparser in _subparsers(parser).items():
        for action in subparser._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            token = action.option_strings[0] if action.option_strings else action.dest
            assert token in doc, (
                f"argument {token!r} of subcommand {name!r} is missing from "
                f"the generated CLI reference"
            )


def test_current_self_target_semantics_are_documented():
    """Lock in the post-inversion semantics. If the CLI ever regresses to
    ``--no-self-target`` the help text changes and these assertions fail,
    forcing a deliberate update rather than silent drift."""
    doc = render_task_cli_reference()
    assert "--self-target" in doc
    assert "--no-self-target" not in doc
    assert "broadcast" in doc.lower()


def test_store_true_flag_has_no_metavar():
    """A boolean flag must render as ``--self-target``, never
    ``--self-target SELF_TARGET`` — otherwise the agent would type a value
    the flag doesn't accept."""
    doc = render_task_cli_reference()
    assert "`--self-target`" in doc
    assert "--self-target SELF_TARGET" not in doc


def test_required_positional_is_marked():
    """``update`` / ``show`` take a positional task id; it must be flagged
    required so the agent doesn't omit it."""
    doc = render_task_cli_reference()
    assert "`task_id`" in doc
    # The bullet for the positional carries the required marker.
    assert "`task_id` *(required)*" in doc


def test_choices_are_rendered():
    """Enumerated options (e.g. --trigger) should expose their choices."""
    doc = render_task_cli_reference()
    assert "--trigger {once|interval|cron}" in doc


def test_run_module_setup_injects_reference_into_system_prompt(tmp_path: Path):
    """End-to-end: run_setup() must drop the CLI reference into the
    MODULES block of the agent's system-prompt file, so a freshly built
    container always carries flags that match the installed core."""
    from superpos_agent_core import run_module_setup

    agents_md = tmp_path / "CLAUDE.md"
    agents_md.write_text(
        "# Agent\n\n"
        "<!-- MODULES:BEGIN -->\n"
        "stale placeholder\n"
        "<!-- MODULES:END -->\n"
    )
    # No workspace modules dir — bundled modules + the CLI reference only.
    run_module_setup(str(tmp_path / "no-such-modules"), str(agents_md))

    content = agents_md.read_text()
    begin = content.index("<!-- MODULES:BEGIN -->")
    end = content.index("<!-- MODULES:END -->")
    block = content[begin:end]

    assert "stale placeholder" not in block
    assert "`superpos-task` CLI" in block
    assert "--self-target" in block
    # The discovered-modules listing still follows the CLI reference.
    assert "Installed Modules" in block


def test_module_runs_as_main_without_runtime_warning():
    """``python -m superpos_agent_core.task_cli_doc`` is the documented way to
    preview the reference. It must run cleanly.

    Re-exporting ``render_task_cli_reference`` from the package ``__init__``
    pulls ``task_cli_doc`` into ``sys.modules`` while runpy imports the parent
    package, so executing the same module as ``__main__`` afterwards trips a
    ``RuntimeWarning`` ("found in sys.modules ... prior to execution"). We keep
    the renderer importable only from the submodule to avoid that; ``-W
    error::RuntimeWarning`` turns any regression into a non-zero exit."""
    result = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m",
         "superpos_agent_core.task_cli_doc"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"running the module as __main__ failed (likely a RuntimeWarning "
        f"escalated to an error):\n{result.stderr}"
    )
    assert "RuntimeWarning" not in result.stderr, result.stderr
    assert "`superpos-task` CLI" in result.stdout


def test_renderer_not_reexported_from_package_root():
    """Guard the fix: the renderer must NOT be re-exported from the package
    root, because the eager import is exactly what triggers the runpy
    RuntimeWarning. Callers import it from the submodule instead."""
    import superpos_agent_core

    assert not hasattr(superpos_agent_core, "render_task_cli_reference")
    assert "render_task_cli_reference" not in superpos_agent_core.__all__
