"""Auto-generate the ``superpos-task`` CLI reference from its argparse parser.

The agent's system-prompt file (``CLAUDE.md`` / ``AGENTS.md``) used to carry
a *hand-written* description of every ``superpos-task`` subcommand and flag.
That prose silently rotted: when the ``create`` flag was inverted from
``--no-self-target`` (default self-target) to ``--self-target`` (default
broadcast), the docs kept describing the old, removed flag — so an agent
following them would type a flag that does nothing.

This module renders the reference straight from the live
:func:`superpos_agent_core.superpos_task.build_parser` object, so the doc is
structurally incapable of drifting from the real flags. It is injected into
the ``<!-- MODULES:BEGIN -->`` block of the system-prompt file at container
startup (see :func:`superpos_agent_core.module_setup.run_setup`).

The argparse introspection touches a few semi-private attributes
(``_actions``, ``_SubParsersAction``, ``_get_subactions``). These have been
stable across Python 3.9–3.13 and are the conventional way to walk a parser;
they are exercised by the tests in ``tests/test_task_cli_doc.py`` so a stdlib
change can't break us silently.
"""

from __future__ import annotations

import argparse


def _metavar(action: argparse.Action) -> str:
    """Best-effort value placeholder for an option that takes an argument."""
    if action.metavar:
        return str(action.metavar)
    if action.choices:
        return "{" + "|".join(str(c) for c in action.choices) + "}"
    return action.dest.upper()


def _takes_value(action: argparse.Action) -> bool:
    """True if the option consumes a value (so it needs a metavar)."""
    # store_true/store_false/store_const/count all set nargs == 0.
    return action.nargs != 0


def _format_invocation(action: argparse.Action) -> str:
    """Render the token the user types, e.g. ``--prompt PROMPT`` or ``task_id``."""
    if not action.option_strings:  # positional
        return str(action.metavar or action.dest)
    opt = action.option_strings[0]
    if _takes_value(action):
        return f"{opt} {_metavar(action)}"
    return opt


def _describe(action: argparse.Action) -> str:
    """One bullet line documenting a single argument."""
    invocation = f"`{_format_invocation(action)}`"

    markers: list[str] = []
    is_positional = not action.option_strings
    # Positionals with default nargs are mandatory; optionals carry an
    # explicit ``required`` flag.
    if (is_positional and action.nargs not in ("?", "*")) or getattr(action, "required", False):
        markers.append("*(required)*")

    help_text = (action.help or "").strip()

    # Surface a default only when it carries information and the help text
    # doesn't already mention one — avoids "(default: 2) (default: 2)".
    default = action.default
    if (
        not is_positional
        and _takes_value(action)
        and default not in (None, False, argparse.SUPPRESS)
        and "default" not in help_text.lower()
    ):
        markers.append(f"(default: `{default}`)")

    suffix = " ".join(markers)
    pieces = [invocation]
    if suffix:
        pieces.append(suffix)
    if help_text:
        pieces.append(f"— {help_text}")
    return "- " + " ".join(pieces)


def _synopsis(subparser: argparse.ArgumentParser) -> str:
    """Collapse argparse's multi-line usage string into a single line."""
    usage = subparser.format_usage()
    usage = usage.replace("usage:", "", 1)
    tokens = [tok for tok in usage.split() if tok != "[-h]"]
    return " ".join(tokens)


def _iter_subparsers(parser: argparse.ArgumentParser):
    """Yield ``(name, subparser, help)`` for each registered subcommand, in order."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            help_by_name = {sub.dest: (sub.help or "") for sub in action._get_subactions()}
            for name, subparser in action.choices.items():
                yield name, subparser, help_by_name.get(name, "")


def render_task_cli_reference(parser: argparse.ArgumentParser | None = None) -> str:
    """Render the full ``superpos-task`` reference as Markdown.

    Pass ``parser`` to render an arbitrary parser (used in tests); by default
    it introspects the real :func:`superpos_agent_core.superpos_task.build_parser`.
    """
    if parser is None:
        # Imported lazily to avoid a circular import at module load time.
        from .superpos_task import build_parser

        parser = build_parser()

    prog = parser.prog
    lines: list[str] = [
        f"## `{prog}` CLI",
        "",
        "Create and manage Superpos tasks, schedules, and persona memory from "
        "inside the agent container. Auto-generated from the live CLI parser — "
        "**do not edit by hand; flags here always match the installed core.**",
        "",
    ]

    for name, subparser, help_text in _iter_subparsers(parser):
        heading = f"### `{prog} {name}`"
        if help_text:
            heading += f" — {help_text}"
        lines.append(heading)
        lines.append("")
        lines.append(f"```\n{_synopsis(subparser)}\n```")
        lines.append("")

        arg_lines = [
            _describe(action)
            for action in subparser._actions
            if not isinstance(action, argparse._HelpAction)
        ]
        if arg_lines:
            lines.extend(arg_lines)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    """Print the rendered reference — handy for previewing the docs locally."""
    print(render_task_cli_reference(), end="")


if __name__ == "__main__":
    main()
