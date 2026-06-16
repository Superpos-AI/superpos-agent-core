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

from superpos_agent_core import skills_loader
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


def test_get_skill_rejects_absolute_name(tmp_path, monkeypatch):
    """An absolute ``name`` must be rejected even when the resolved
    ``<name>.md`` file actually exists on disk — otherwise the helper
    could be coerced into reading arbitrary files outside the bundled dir.

    ``Path(root) / "/abs/secret.md"`` discards ``root`` and yields
    ``/abs/secret.md``; validating against ``list_bundled_skills()`` first
    closes that escape.
    """
    # A hermetic bundled dir holding one real slug.
    bundled = tmp_path / "skills"
    bundled.mkdir()
    (bundled / "plan.md").write_text("name: plan\n", encoding="utf-8")
    monkeypatch.setattr(skills_loader, "bundled_skills_dir", lambda: str(bundled))

    # An absolute path whose ``<name>.md`` genuinely exists on disk.
    secret = tmp_path / "secret"
    secret_md = tmp_path / "secret.md"
    secret_md.write_text("top secret", encoding="utf-8")
    assert secret_md.is_file()

    with pytest.raises(FileNotFoundError):
        get_skill(str(secret))


def test_get_skill_rejects_dotdot_name(tmp_path, monkeypatch):
    """A ``../``-relative ``name`` must be rejected even when the escaped
    target ``.md`` exists one level above the bundled dir."""
    bundled = tmp_path / "skills"
    bundled.mkdir()
    (bundled / "plan.md").write_text("name: plan\n", encoding="utf-8")
    monkeypatch.setattr(skills_loader, "bundled_skills_dir", lambda: str(bundled))

    # Create the file that ``../escape`` would resolve to (sibling of bundled).
    escape_md = tmp_path / "escape.md"
    escape_md.write_text("escaped", encoding="utf-8")
    assert (bundled / "../escape.md").resolve().is_file()

    with pytest.raises(FileNotFoundError):
        get_skill("../escape")


def test_get_skill_accepts_valid_slug(tmp_path, monkeypatch):
    """A normal bundled slug still resolves to its on-disk path."""
    bundled = tmp_path / "skills"
    bundled.mkdir()
    (bundled / "plan.md").write_text("name: plan\n", encoding="utf-8")
    monkeypatch.setattr(skills_loader, "bundled_skills_dir", lambda: str(bundled))

    assert "plan" in list_bundled_skills()
    path = get_skill("plan")
    assert Path(path) == bundled / "plan.md"
    assert Path(path).is_file()
