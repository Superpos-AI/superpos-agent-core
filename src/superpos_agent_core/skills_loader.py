"""Discover the skills shipped inside the ``superpos_agent_core`` package.

Skills are **flat ``<slug>.md`` markdown files** bundled under ``skills/``
— the same enforced runtime contract used by ``registry_overlay.overlay_skills``
(which writes ``<skills_dir>/<slug>.md``) and ``sub_agent_sync`` (which exposes
``entry.stem`` as ``/skill-name``).  They are platform-level skills (plan,
review, summarize) that every Superpos agent gets for free — defined once,
shared across Claude / Codex / Gemini / Qwen.

This mirrors :mod:`superpos_agent_core.module_loader`: a tiny, dependency-free
(stdlib ``pathlib`` only) helper that resolves the bundled root off
``__file__`` and lists / fetches individual entries.
"""

from __future__ import annotations

from pathlib import Path


def bundled_skills_dir() -> str:
    """Path to the skills directory shipped inside this package.

    Returned even if the directory doesn't exist yet so callers can decide
    how to handle a stripped install — parallels
    :func:`module_loader.bundled_modules_dir`.
    """
    return str(Path(__file__).parent / "skills")


def list_bundled_skills() -> list[str]:
    """Sorted slugs (filename stems) of every ``*.md`` in the bundled dir.

    A missing bundled directory is treated as "no bundled skills" rather
    than an error, mirroring the loader's tolerance of a stripped install.
    """
    root = Path(bundled_skills_dir())
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.glob("*.md") if p.is_file())


def get_skill(name: str) -> str:
    """Return the absolute on-disk path to the bundled ``<name>.md`` skill.

    Raises :class:`FileNotFoundError` with a helpful message listing the
    available skills if no such skill is bundled.
    """
    path = Path(bundled_skills_dir()) / f"{name}.md"
    if not path.is_file():
        available = ", ".join(list_bundled_skills()) or "(none)"
        raise FileNotFoundError(
            f"no bundled skill named {name!r}; available skills: {available}"
        )
    return str(path)
