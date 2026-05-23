"""Tests for the bundled+workspace module discovery merge.

The bundled root lives inside the installed ``superpos_agent_core`` package
under ``modules/``.  ``discover_modules`` is expected to:

- pick up every module from the bundled root automatically,
- merge in workspace modules from the caller-provided ``modules_dir``,
- let a workspace module shadow a bundled one of the same name.

The accompanying ``symlink_module_scripts`` helper is exercised in a tmp
``bin`` dir so we don't touch ``$PATH`` or the real ``modules-bin``.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

from superpos_agent_core import (
    bundled_modules_dir,
    discover_modules,
    symlink_module_scripts,
)


def _write_module(root: Path, name: str, description: str, scripts: dict[str, str] | None = None) -> Path:
    """Materialise a module directory ``root/name/`` with a yaml + optional scripts."""
    mod = root / name
    (mod / "scripts").mkdir(parents=True, exist_ok=True)
    (mod / "module.yaml").write_text(
        textwrap.dedent(
            f"""\
            description: "{description}"
            env: []
            """
        )
    )
    for script_name, body in (scripts or {}).items():
        path = mod / "scripts" / script_name
        path.write_text(body)
        path.chmod(0o755)
    return mod


def test_bundled_modules_dir_resolves_inside_package():
    """The bundled root must live inside the installed package — that's
    how `pip install` ships it. A path that points elsewhere would mean
    the package_data glob in pyproject.toml is mis-configured."""
    bundled = Path(bundled_modules_dir())
    # The path is computed off __file__, so we just sanity-check the
    # trailing component and that it is a child of the package.
    assert bundled.name == "modules"
    assert "superpos_agent_core" in bundled.parts


def test_discover_includes_bundled_superpos_issues():
    """The new platform module must show up via discovery with no caller
    workspace at all — this is the whole point of bundling."""
    modules = discover_modules(modules_dir=None)
    names = [m.name for m in modules]
    assert "superpos-issues" in names

    issues_mod = next(m for m in modules if m.name == "superpos-issues")
    assert "issue" in issues_mod.description.lower()
    assert "superpos-issues" in issues_mod.scripts


def test_workspace_module_does_not_evict_bundled(tmp_path: Path):
    """A workspace module with a different name must coexist with bundled ones."""
    workspace = tmp_path / "modules"
    workspace.mkdir()
    _write_module(workspace, "custom-tool", "Project-specific helper")

    modules = discover_modules(str(workspace))
    names = {m.name for m in modules}
    assert "superpos-issues" in names  # bundled survives
    assert "custom-tool" in names      # workspace added


def test_workspace_override_shadows_bundled(tmp_path: Path):
    """When workspace and bundled both define a module of the same name,
    the workspace version must win — that's how an individual agent
    customises a platform module without forking core."""
    workspace = tmp_path / "modules"
    workspace.mkdir()
    _write_module(
        workspace, "superpos-issues",
        "Custom issues override for this agent",
    )

    modules = discover_modules(str(workspace))
    issues_mod = next(m for m in modules if m.name == "superpos-issues")
    # Description came from the workspace file, not the bundled one.
    assert issues_mod.description == "Custom issues override for this agent"
    # Path points at the workspace copy.
    assert issues_mod.path == str(workspace / "superpos-issues")


def test_include_bundled_false_returns_workspace_only(tmp_path: Path):
    """Tests and edge cases sometimes need the legacy single-root behaviour."""
    workspace = tmp_path / "modules"
    workspace.mkdir()
    _write_module(workspace, "only-mine", "Local-only module")

    modules = discover_modules(str(workspace), include_bundled=False)
    names = {m.name for m in modules}
    assert names == {"only-mine"}


def test_symlink_module_scripts_links_bundled_and_workspace(tmp_path: Path):
    """The symlink helper must walk both roots so bundled scripts end up
    on ``$PATH`` alongside workspace ones, with workspace winning on
    basename conflict."""
    workspace = tmp_path / "modules"
    workspace.mkdir()
    _write_module(
        workspace, "custom-tool", "Project tool",
        scripts={"custom-tool-cli": "#!/usr/bin/env bash\necho hi\n"},
    )

    bin_dir = tmp_path / "bin"
    symlink_module_scripts(str(workspace), str(bin_dir))

    # Workspace script is linked.
    custom_link = bin_dir / "custom-tool-cli"
    assert custom_link.is_symlink()
    assert custom_link.resolve() == (workspace / "custom-tool/scripts/custom-tool-cli").resolve()

    # Bundled superpos-issues script is also linked, even though the
    # caller never mentioned it.
    issues_link = bin_dir / "superpos-issues"
    assert issues_link.is_symlink()
    assert "superpos-issues" in str(issues_link.resolve())


def test_symlink_module_scripts_workspace_wins_on_name_conflict(tmp_path: Path):
    """A workspace script named the same as a bundled one must replace
    the bundled symlink — last-writer-wins, with workspace last."""
    workspace = tmp_path / "modules"
    workspace.mkdir()
    # Shadow the bundled `superpos-issues` script with a stub of our own.
    _write_module(
        workspace, "superpos-issues", "override",
        scripts={"superpos-issues": "#!/usr/bin/env bash\necho workspace-wins\n"},
    )

    bin_dir = tmp_path / "bin"
    symlink_module_scripts(str(workspace), str(bin_dir))

    link = bin_dir / "superpos-issues"
    assert link.is_symlink()
    # Resolves to the workspace copy, not the bundled one inside the
    # installed package.
    assert str(link.resolve()).startswith(str(workspace))


def test_symlink_module_scripts_handles_missing_workspace(tmp_path: Path):
    """A None workspace path is fine — we still link bundled scripts so
    the agent gets platform tools even when nothing is configured."""
    bin_dir = tmp_path / "bin"
    symlink_module_scripts(None, str(bin_dir))

    issues_link = bin_dir / "superpos-issues"
    assert issues_link.is_symlink()


def test_symlink_module_scripts_replaces_stale_link(tmp_path: Path):
    """Subsequent runs (container restart) should overwrite an existing
    symlink rather than fail — entrypoint reruns are common."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    stale = bin_dir / "superpos-issues"
    stale.symlink_to("/nonexistent/path")

    symlink_module_scripts(None, str(bin_dir))

    assert stale.is_symlink()
    assert os.readlink(stale) != "/nonexistent/path"


def test_symlink_module_scripts_handles_relative_modules_dir(tmp_path: Path, monkeypatch):
    """Codex P2: when ``modules_dir`` is passed as a relative path (the
    natural CLI invocation, e.g. ``--modules-dir .codex/modules``), the
    individual script Paths are also relative.  ``Path.symlink_to``
    stores the target literally and resolves it relative to the symlink
    *location* (``bin_dir``), so a naive relative target produces a
    broken link.  The helper must resolve to absolute before linking.
    """
    # Build the layout under a workdir, then chdir there and pass a
    # relative path — mirrors how `python3 -m superpos_agent_core.module_setup`
    # is typically invoked from a project root.
    workdir = tmp_path
    rel_modules = "myagent/modules"
    workspace = workdir / rel_modules
    workspace.mkdir(parents=True)
    _write_module(
        workspace, "demo", "Demo module",
        scripts={"demo-cli": "#!/usr/bin/env bash\necho ok\n"},
    )

    bin_dir = workdir / "bin"
    monkeypatch.chdir(workdir)

    symlink_module_scripts(rel_modules, str(bin_dir))

    link = bin_dir / "demo-cli"
    assert link.is_symlink()
    # Critical assertion: the symlink target is absolute and actually
    # resolves to a real file.  A relative target stored verbatim would
    # resolve relative to ``bin_dir`` and point at a non-existent path.
    target = Path(os.readlink(link))
    assert target.is_absolute(), f"target should be absolute, got {target!r}"
    assert link.resolve().is_file()
