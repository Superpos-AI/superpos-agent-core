"""Tests for the bundled skills loader.

The bundled root lives inside the installed ``superpos_agent_core`` package
under ``skills/`` as flat ``<slug>.md`` files — the same shape
``registry_overlay.overlay_skills`` writes and ``sub_agent_sync`` exposes via
``entry.stem``.  ``skills_loader`` is expected to:

- resolve the bundled root inside the installed package,
- list every bundled skill by its filename stem,
- return the on-disk path for a named skill and raise for an unknown one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from superpos_agent_core.skills_loader import (
    bundled_skills_dir,
    get_skill,
    list_bundled_skills,
)


def test_bundled_skills_dir_resolves_inside_package():
    """The bundled root must live inside the installed package — that's
    how `pip install` ships it. A path elsewhere would mean the
    package_data glob in pyproject.toml is mis-configured."""
    bundled = Path(bundled_skills_dir())
    assert bundled.name == "skills"
    assert "superpos_agent_core" in bundled.parts


def test_list_bundled_skills_returns_canonical_three():
    """The three canonical platform skills must ship bundled, sorted."""
    assert list_bundled_skills() == ["plan", "review", "summarize"]


def test_get_skill_returns_existing_path_with_frontmatter():
    """get_skill() returns an on-disk path; the file must carry the
    expected `name:` frontmatter so packaging didn't truncate it."""
    path = get_skill("plan")
    p = Path(path)
    assert p.is_file()
    assert "name: plan" in p.read_text(encoding="utf-8")


def test_get_skill_unknown_raises():
    """An unknown skill name raises with a helpful message."""
    with pytest.raises(FileNotFoundError):
        get_skill("does-not-exist")
