"""Tests for the Beat 2b registry overlay (registry_overlay + module_setup wiring).

Covers the proposal's required behaviours:

- Flag OFF → zero registry use, baked-in path identical to today (the
  instant-rollback guarantee).
- Flag ON, registry returns modules + skills → installed (scripts
  symlinked, skills written, CLAUDE.md doc blocks present), with overlay
  precedence (registry wins on slug collision).
- Fetch failure → fall back to baked-in, startup completes, warning logged.
- Per-module install failure → retried once, skipped,
  ``registry.module_install_failed`` logged, other modules still installed.
- ``env_keys`` are treated as NAMES only — no credential value injected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from superpos_agent_core import module_setup
from superpos_agent_core.registry_overlay import (
    FEATURE_FLAG_ENV,
    MODULE_INSTALL_FAILED_EVENT,
    apply_registry_overlay,
    feature_enabled,
    overlay_modules,
)

BEGIN = module_setup.BEGIN_MARKER
END = module_setup.END_MARKER


# ── Fixtures / helpers ───────────────────────────────────────────────


def _resolved_payload():
    """A resolved payload with 1 skill + 1 module (overlay onto baked-in)."""
    return {
        "items": [],
        "skills": [
            {
                "slug": "deep-research",
                "name": "Deep Research",
                "instructions": "# deep-research\n\nResearch harness.\n",
                "files": [
                    {"path": "scripts/run.sh", "content": "echo hi\n", "mode": "+x"},
                ],
            },
        ],
        "modules": [
            {
                "slug": "registry-only-mod",
                "name": "Registry Only Module",
                "manifest": {
                    "description": "A module served only by the registry",
                    "env_keys": ["SOME_TOKEN", "OTHER_KEY"],
                    "scripts": ["regmod-cli"],
                },
                "files": [
                    {
                        "path": "scripts/regmod-cli",
                        "content": "#!/usr/bin/env bash\necho registry\n",
                        "mode": "+x",
                    },
                ],
                "skill": "# regmod\n\nuse regmod-cli\n",
            },
        ],
        "subagents": [],
    }


def _agents_md(tmp_path: Path) -> Path:
    p = tmp_path / "CLAUDE.md"
    p.write_text(f"# Agent\n\n{BEGIN}\n(placeholder)\n{END}\n")
    return p


# ── Flag default-off / parsing ───────────────────────────────────────


def test_flag_defaults_off():
    assert feature_enabled({}) is False
    assert feature_enabled({FEATURE_FLAG_ENV: ""}) is False
    assert feature_enabled({FEATURE_FLAG_ENV: "false"}) is False
    assert feature_enabled({FEATURE_FLAG_ENV: "0"}) is False


def test_flag_truthy_values():
    for v in ("1", "true", "TRUE", "yes", "on"):
        assert feature_enabled({FEATURE_FLAG_ENV: v}) is True


# ── Flag OFF: zero behaviour change, no registry use ─────────────────


def test_flag_off_apply_overlay_is_noop(tmp_path: Path, monkeypatch):
    """Flag OFF → apply_registry_overlay touches nothing and reports skipped.

    This is the instant-rollback guarantee proof at the overlay level:
    even handed a fully-populated payload, with the flag off nothing is
    written.
    """
    monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)
    skills_dir = tmp_path / "skills"
    modules_dir = tmp_path / "modules"

    result = apply_registry_overlay(
        _resolved_payload(),
        modules_dir=str(modules_dir),
        skills_dir=str(skills_dir),
    )

    assert result.skipped is True
    assert not skills_dir.exists()
    assert not modules_dir.exists()


def test_run_setup_flag_off_never_fetches_and_matches_baked_in(
    tmp_path: Path, monkeypatch
):
    """run_setup with the flag OFF must not call the registry fetch and must
    leave only baked-in modules in the doc — identical to today."""
    monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)

    # Sentinel: if the overlay path ever fetches with the flag off, fail.
    called = {"fetch": False}

    def _boom():
        called["fetch"] = True
        raise AssertionError("registry fetch must not happen when flag is off")

    monkeypatch.setattr(module_setup, "_fetch_registry_resolved", _boom)

    modules_dir = tmp_path / "modules"  # no workspace modules
    skills_dir = tmp_path / "skills"
    agents_md = _agents_md(tmp_path)

    module_setup.run_setup(
        str(modules_dir),
        str(agents_md),
        bin_dir=str(tmp_path / "bin"),
        registry_resolved=_resolved_payload(),  # ignored when flag off
        skills_dir=str(skills_dir),
    )

    assert called["fetch"] is False
    doc = agents_md.read_text()
    # Baked-in modules present; the registry-only module is NOT installed.
    assert "superpos-github" in doc
    assert "registry-only-mod" not in doc
    assert not (skills_dir / "deep-research.md").exists()


def test_run_setup_flag_off_doc_identical_with_and_without_payload(
    tmp_path: Path, monkeypatch
):
    """The rendered module doc must be byte-identical whether or not a
    registry payload is supplied, proving the payload is fully inert when
    the flag is off."""
    monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)

    def _render(with_payload: bool) -> str:
        agents_md = tmp_path / f"CLAUDE_{with_payload}.md"
        agents_md.write_text(f"# A\n\n{BEGIN}\nx\n{END}\n")
        module_setup.run_setup(
            str(tmp_path / "modules"),
            str(agents_md),
            skills_dir=str(tmp_path / "skills"),
            registry_resolved=_resolved_payload() if with_payload else None,
        )
        return agents_md.read_text()

    assert _render(True) == _render(False)


# ── Flag ON: install skills + modules, overlay precedence ────────────


def test_flag_on_installs_skills_and_modules(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    skills_dir = tmp_path / "skills"
    bin_dir = tmp_path / "bin"
    agents_md = _agents_md(tmp_path)

    module_setup.run_setup(
        str(modules_dir),
        str(agents_md),
        bin_dir=str(bin_dir),
        registry_resolved=_resolved_payload(),
        skills_dir=str(skills_dir),
    )

    # Skill written + its file materialised + executable.
    skill_md = skills_dir / "deep-research.md"
    assert skill_md.is_file()
    assert "Research harness" in skill_md.read_text()
    skill_script = skills_dir / "deep-research" / "scripts" / "run.sh"
    assert skill_script.is_file()
    assert os.access(skill_script, os.X_OK)

    # Module materialised into workspace modules dir.
    mod_yaml = modules_dir / "registry-only-mod" / "module.yaml"
    assert mod_yaml.is_file()

    # Module script symlinked onto PATH dir + executable.
    link = bin_dir / "regmod-cli"
    assert link.is_symlink()
    assert os.access(link.resolve(), os.X_OK)

    # Doc block contains BOTH the registry module and the baked-in ones.
    doc = agents_md.read_text()
    assert "registry-only-mod" in doc
    assert "superpos-github" in doc  # baked-in not in registry → remains


def test_flag_on_registry_module_wins_on_slug_collision(tmp_path: Path, monkeypatch):
    """A registry module with the same slug as a baked-in (bundled) one
    overlays/replaces it — registry wins."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    skills_dir = tmp_path / "skills"
    agents_md = _agents_md(tmp_path)

    payload = {
        "skills": [],
        "modules": [
            {
                "slug": "superpos-github",  # collides with bundled
                "name": "Overridden GitHub",
                "manifest": {
                    "description": "REGISTRY OVERRIDE of github",
                    "env_keys": ["GITHUB_TOKEN"],
                },
                "files": [],
            }
        ],
    }

    module_setup.run_setup(
        str(modules_dir),
        str(agents_md),
        registry_resolved=payload,
        skills_dir=str(skills_dir),
    )

    # Workspace copy now shadows the bundled one (discover_modules merges
    # workspace over bundled), so the description in the doc is the
    # registry override.
    doc = agents_md.read_text()
    assert "REGISTRY OVERRIDE of github" in doc
    mod_yaml = (modules_dir / "superpos-github" / "module.yaml").read_text()
    assert "REGISTRY OVERRIDE of github" in mod_yaml


def test_flag_on_registry_skill_wins_on_slug_collision(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    # A pre-existing baked-in skill of the same slug.
    (skills_dir / "deep-research.md").write_text("# OLD baked-in\n")

    apply_registry_overlay(
        {"skills": _resolved_payload()["skills"], "modules": []},
        modules_dir=str(tmp_path / "modules"),
        skills_dir=str(skills_dir),
    )

    content = (skills_dir / "deep-research.md").read_text()
    assert "OLD baked-in" not in content
    assert "Research harness" in content  # registry won


def test_flag_on_baked_in_skill_not_in_registry_remains(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "untouched.md").write_text("# keep me\n")

    apply_registry_overlay(
        {"skills": _resolved_payload()["skills"], "modules": []},
        modules_dir=str(tmp_path / "modules"),
        skills_dir=str(skills_dir),
    )

    assert (skills_dir / "untouched.md").read_text() == "# keep me\n"
    assert (skills_dir / "deep-research.md").is_file()


# ── env_keys are names only ──────────────────────────────────────────


def test_env_keys_are_names_only_no_value_injection(tmp_path: Path):
    """The module manifest's env_keys land as plain NAMES in module.yaml's
    ``env`` list — never key=value, never resolved values."""
    modules_dir = tmp_path / "modules"
    overlay_modules(_resolved_payload()["modules"], str(modules_dir))

    import yaml

    data = yaml.safe_load(
        (modules_dir / "registry-only-mod" / "module.yaml").read_text()
    )
    assert data["env"] == ["SOME_TOKEN", "OTHER_KEY"]
    # No value-bearing shapes anywhere.
    raw = (modules_dir / "registry-only-mod" / "module.yaml").read_text()
    assert "SOME_TOKEN=" not in raw
    assert ": SOME_TOKEN" not in raw or raw.count("SOME_TOKEN") == 1


# ── Fetch failure → baked-in fallback ────────────────────────────────


def test_fetch_failure_falls_back_to_baked_in(tmp_path: Path, monkeypatch, caplog):
    """resolved=None (fetch failed) with flag ON → degrade to baked-in,
    startup completes, warning logged, baked-in modules still documented."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    skills_dir = tmp_path / "skills"
    agents_md = _agents_md(tmp_path)

    with caplog.at_level("WARNING"):
        module_setup.run_setup(
            str(modules_dir),
            str(agents_md),
            registry_resolved=None,  # simulate fetch failure
            skills_dir=str(skills_dir),
        )

    # Baked-in modules still present (agent started fine).
    doc = agents_md.read_text()
    assert "superpos-github" in doc
    # No registry skill written.
    assert not (skills_dir / "deep-research.md").exists()
    assert any("falling back" in r.message.lower() for r in caplog.records)


def test_run_setup_does_not_raise_on_fetch_failure(tmp_path: Path, monkeypatch):
    """A degraded fetch must never crash startup."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    # Should simply complete.
    module_setup.run_setup(
        str(tmp_path / "modules"),
        str(_agents_md(tmp_path)),
        registry_resolved=None,
        skills_dir=str(tmp_path / "skills"),
    )


# ── Per-module install failure → retry once, skip, log, continue ─────


def test_module_install_failure_retried_skipped_logged_others_continue(
    tmp_path: Path, caplog
):
    """A module whose materialisation raises is retried once, then skipped
    with a registry.module_install_failed record — and the other modules
    still install."""
    modules_dir = tmp_path / "modules"

    good = {
        "slug": "good-mod",
        "name": "Good",
        "manifest": {"description": "ok", "env_keys": []},
        "files": [],
    }
    bad = {
        "slug": "bad-mod",
        "name": "Bad",
        "manifest": {"description": "boom", "env_keys": []},
        "files": [{"path": "scripts/x", "content": "x", "mode": "+x"}],
    }

    sleeps: list[float] = []
    attempts = {"bad-mod": 0}

    real_write = Path.write_text

    def flaky_write(self, *args, **kwargs):
        # Fail only when writing bad-mod's module.yaml.
        if "bad-mod" in str(self) and self.name == "module.yaml":
            attempts["bad-mod"] += 1
            raise OSError("disk gremlin")
        return real_write(self, *args, **kwargs)

    import unittest.mock as mock

    with mock.patch.object(Path, "write_text", flaky_write), caplog.at_level(
        "WARNING"
    ):
        result = overlay_modules(
            [good, bad],
            str(modules_dir),
            backoff_seconds=0.01,
            sleep=lambda s: sleeps.append(s),
        )

    # Bad module attempted twice (1 + 1 retry), then skipped.
    assert attempts["bad-mod"] == 2
    assert len(sleeps) == 1  # one bounded backoff between the two attempts
    assert "bad-mod" in result.failed
    assert "bad-mod" not in result.installed

    # Good module still installed.
    assert "good-mod" in result.installed
    assert (modules_dir / "good-mod" / "module.yaml").is_file()

    # Structured failure record emitted.
    records = [r for r in caplog.records if MODULE_INSTALL_FAILED_EVENT in r.message]
    assert records, "expected a registry.module_install_failed log record"
    assert any("bad-mod" in r.message for r in records)


def test_skipped_module_not_symlinked(tmp_path: Path, monkeypatch):
    """A module that fails to install must not have its scripts symlinked
    onto PATH (it was never materialised)."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    skills_dir = tmp_path / "skills"
    bin_dir = tmp_path / "bin"
    agents_md = _agents_md(tmp_path)

    # An unsafe slug guarantees _materialise_module raises → skipped.
    payload = {
        "skills": [],
        "modules": [
            {
                "slug": "../escape",
                "name": "Evil",
                "manifest": {"description": "x", "env_keys": []},
                "files": [{"path": "scripts/evil", "content": "x", "mode": "+x"}],
            }
        ],
    }

    module_setup.run_setup(
        str(modules_dir),
        str(agents_md),
        bin_dir=str(bin_dir),
        registry_resolved=payload,
        skills_dir=str(skills_dir),
    )

    # No escape happened; no evil script on PATH.
    assert not (bin_dir / "evil").exists()
    # Nothing escaped the modules dir either.
    assert not (tmp_path / "escape").exists()
