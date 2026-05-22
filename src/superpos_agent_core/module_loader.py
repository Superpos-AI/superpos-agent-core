"""Discover and load module metadata.

Modules come from two roots:

1. **Bundled** — shipped inside the ``superpos_agent_core`` Python package
   under ``modules/``.  These are platform-level tools (knowledge, issues,
   ...) that every Superpos agent gets for free — defined once, shared
   across Claude / Codex / Gemini / Qwen.

2. **Workspace** — the per-agent directory (typically
   ``$WORKDIR/.<agent>/modules/``) where agent-specific extensions live.
   A workspace module with the same name as a bundled one wins, so an
   individual agent can still override or shadow a platform module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ModuleInfo:
    name: str
    description: str
    path: str
    scripts: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    has_mcp: bool = False
    mcp_config: dict | None = None


def bundled_modules_dir() -> str:
    """Path to the modules directory shipped inside this package.

    Returned even if the directory doesn't exist yet so callers can decide
    how to handle a stripped install — :func:`discover_modules` treats a
    missing path as "no bundled modules" rather than an error.
    """
    return str(Path(__file__).parent / "modules")


def _load_one(entry: Path) -> ModuleInfo | None:
    """Parse a single module directory into :class:`ModuleInfo`.

    Returns ``None`` if the entry is not a valid module (missing
    ``module.yaml``), so callers can ``filter(None, ...)`` over the result.
    """
    yaml_path = entry / "module.yaml"
    if not yaml_path.exists():
        return None

    with open(yaml_path) as f:
        meta = yaml.safe_load(f) or {}

    scripts: list[str] = []
    scripts_dir = entry / "scripts"
    if scripts_dir.is_dir():
        scripts = sorted(p.name for p in scripts_dir.iterdir() if p.is_file())

    mcp_config = meta.get("mcp")
    return ModuleInfo(
        name=entry.name,
        description=meta.get("description", ""),
        path=str(entry),
        scripts=scripts,
        env_vars=meta.get("env", []),
        has_mcp=mcp_config is not None,
        mcp_config=mcp_config,
    )


def discover_modules(
    modules_dir: str | None,
    *,
    include_bundled: bool = True,
) -> list[ModuleInfo]:
    """Scan module directories, parse module.yaml, return metadata.

    With ``include_bundled=True`` (default), the package-bundled modules
    are merged in first and then overlaid with anything in ``modules_dir``.
    The merge is by module name — a workspace module shadows a bundled one
    with the same directory name.  Pass ``include_bundled=False`` for the
    legacy single-root behaviour (useful in tests).
    """
    roots: list[Path] = []
    if include_bundled:
        bundled = Path(bundled_modules_dir())
        if bundled.is_dir():
            roots.append(bundled)
    if modules_dir:
        workspace = Path(modules_dir)
        if workspace.is_dir():
            roots.append(workspace)

    by_name: dict[str, ModuleInfo] = {}
    for root in roots:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            info = _load_one(entry)
            if info is None:
                continue
            # Later root overrides earlier — workspace wins over bundled.
            by_name[info.name] = info

    return [by_name[name] for name in sorted(by_name)]


def generate_modules_doc(modules: list[ModuleInfo]) -> str:
    """Generate markdown listing all modules and their scripts."""
    if not modules:
        return "No modules installed.\n"

    lines = ["## Installed Modules\n"]
    for mod in modules:
        lines.append(f"### {mod.name}\n")
        lines.append(f"{mod.description}\n")

        if mod.scripts:
            lines.append("**Scripts** (available on PATH):\n")
            for script in mod.scripts:
                lines.append(f"- `{script}`")
            lines.append("")

        if mod.env_vars:
            lines.append(
                f"**Environment** (pre-configured, do NOT echo or print these): "
                f"{', '.join(f'`{v}`' for v in mod.env_vars)}\n"
            )

        skill_path = os.path.join(mod.path, "SKILL.md")
        if os.path.exists(skill_path):
            lines.append(f"**Skill**: See `{skill_path}`\n")

    return "\n".join(lines)


def collect_mcp_servers(modules: list[ModuleInfo]) -> dict:
    """Merge all module MCP configs into one dict."""
    merged: dict = {}
    for mod in modules:
        if mod.has_mcp and mod.mcp_config:
            merged.update(mod.mcp_config)
    return merged
