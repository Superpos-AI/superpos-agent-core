"""Regression: the packaging config must ship the bundled persona/memory
snapshot floor.

``persona_overlay.bundled_snapshot_dir()`` reads ``snapshots/*.md`` (persona.md
and MEMORY.md) as the never-online / outage fallback. If ``pyproject.toml``
package-data drops that pattern, a built wheel omits those files and an offline
agent loses its fallback floor. See PR #58 review.
"""

import fnmatch
import re
from pathlib import Path

from superpos_agent_core.persona_overlay import bundled_snapshot_dir

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "src" / "superpos_agent_core"


def _package_data_patterns() -> list[str]:
    """Patterns declared for the ``superpos_agent_core`` package in pyproject.

    Parsed with a stdlib regex (no ``tomllib``) so the test runs identically on
    Python 3.10 through 3.13.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r"superpos_agent_core\s*=\s*\[(.*?)\]", text, re.DOTALL)
    assert match, "package-data list for superpos_agent_core not found in pyproject.toml"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_bundled_snapshot_files_are_declared_as_package_data() -> None:
    patterns = _package_data_patterns()
    snapshot_files = sorted(bundled_snapshot_dir().glob("*.md"))
    assert snapshot_files, "expected bundled snapshot .md files to exist under snapshots/"
    for path in snapshot_files:
        rel = path.relative_to(PACKAGE_ROOT).as_posix()
        assert any(fnmatch.fnmatch(rel, pattern) for pattern in patterns), (
            f"{rel} is not matched by any package-data pattern {patterns}; a built "
            "wheel would omit the never-online persona/memory snapshot fallback"
        )
