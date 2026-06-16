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
    RESOLVED_EMPTY_NO_FALLBACK_EVENT,
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


# ── Flag default-on / parsing ────────────────────────────────────────


def test_flag_defaults_on():
    # Default-ON: unset / empty enables the overlay.
    assert feature_enabled({}) is True
    assert feature_enabled({FEATURE_FLAG_ENV: ""}) is True


def test_flag_explicit_falsey_disables():
    # The rollback path: only an explicit falsey value disables the overlay.
    for v in ("0", "false", "FALSE", "no", "off", "  off  ", "Off"):
        assert feature_enabled({FEATURE_FLAG_ENV: v}) is False


def test_flag_truthy_values():
    for v in ("1", "true", "TRUE", "yes", "on", "  on  "):
        assert feature_enabled({FEATURE_FLAG_ENV: v}) is True


# ── Flag OFF: zero behaviour change, no registry use ─────────────────


def test_flag_off_apply_overlay_is_noop(tmp_path: Path, monkeypatch):
    """Flag OFF → apply_registry_overlay touches nothing and reports skipped.

    This is the instant-rollback guarantee proof at the overlay level:
    even handed a fully-populated payload, with the flag explicitly off
    nothing is written.
    """
    monkeypatch.setenv(FEATURE_FLAG_ENV, "false")
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
    """run_setup with the flag explicitly OFF must not call the registry
    fetch and must leave only baked-in modules in the doc — identical to
    today."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "false")

    # Sentinel: if the overlay path ever fetches with the flag off, fail.
    called = {"fetch": False}

    def _boom(*args, **kwargs):
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
    the flag is explicitly off."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "false")

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
    # The overlay re-render must KEEP the superpos-task CLI reference that
    # run_setup prepended — it must not be replaced by module docs only.
    assert "`superpos-task` CLI" in doc
    assert "--self-target" in doc


def test_registry_overlay_preserves_task_cli_reference(tmp_path: Path, monkeypatch):
    """Regression for the overlay dropping the CLI reference.

    When the flag is ON and the resolved payload contains a registry module,
    run_setup prepends the superpos-task CLI reference and then the overlay
    re-renders the MODULES block.  That re-render must include the CLI
    reference, not module docs only — otherwise the anti-drift doc vanishes
    on every registry-backed startup.
    """
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    skills_dir = tmp_path / "skills"
    agents_md = _agents_md(tmp_path)

    module_setup.run_setup(
        str(modules_dir),
        str(agents_md),
        bin_dir=str(tmp_path / "bin"),
        registry_resolved=_resolved_payload(),
        skills_dir=str(skills_dir),
    )

    content = agents_md.read_text()
    begin = content.index(BEGIN)
    end = content.index(END)
    block = content[begin:end]

    # The CLI reference survived the overlay re-render…
    assert "`superpos-task` CLI" in block
    assert "--self-target" in block
    # …and it still leads the block, with the modules listing after it.
    assert block.index("`superpos-task` CLI") < block.index("Installed Modules")
    # The registry module is also present (overlay actually ran).
    assert "registry-only-mod" in block


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


def test_successful_update_replaces_existing_module(tmp_path: Path):
    """A successful re-install over an existing module dir replaces its
    contents (no stale files survive the swap)."""
    modules_dir = tmp_path / "modules"

    v1 = {
        "slug": "upgradable",
        "name": "Upgradable",
        "manifest": {"description": "v1", "env_keys": []},
        "files": [
            {"path": "scripts/cli", "content": "echo v1\n", "mode": "+x"},
            {"path": "stale.txt", "content": "old\n"},
        ],
    }
    overlay_modules([v1], str(modules_dir))
    install_dir = modules_dir / "upgradable"
    assert (install_dir / "stale.txt").is_file()

    v2 = {
        "slug": "upgradable",
        "name": "Upgradable",
        "manifest": {"description": "v2", "env_keys": []},
        "files": [{"path": "scripts/cli", "content": "echo v2\n", "mode": "+x"}],
    }
    result = overlay_modules([v2], str(modules_dir))

    assert "upgradable" in result.installed
    assert "v2" in (install_dir / "module.yaml").read_text()
    assert "echo v2" in (install_dir / "scripts" / "cli").read_text()
    # The atomic swap means no stale file from v1 survives.
    assert not (install_dir / "stale.txt").exists()


def test_failed_update_preserves_existing_install(tmp_path: Path, caplog):
    """An UPDATE over an existing installed module that fails to materialise
    must leave the OLD module directory intact (the skip/fallback guarantee),
    report the slug in ``.failed`` (not ``.installed``), and leave behind no
    leftover ``.<slug>.tmp-*`` staging directory."""
    modules_dir = tmp_path / "modules"

    # Install a working v1 with a script file that must survive a failed update.
    v1 = {
        "slug": "upgradable",
        "name": "Upgradable",
        "manifest": {"description": "v1-keep-me", "env_keys": ["KEEP_KEY"]},
        "files": [
            {"path": "scripts/cli", "content": "echo v1\n", "mode": "+x"},
            {"path": "SKILL.md", "content": "# v1 skill\n"},
        ],
    }
    overlay_modules([v1], str(modules_dir))
    install_dir = modules_dir / "upgradable"
    assert install_dir.is_dir()
    script = install_dir / "scripts" / "cli"
    assert script.is_file()

    # Now attempt a v2 update whose write raises while materialising.
    v2 = {
        "slug": "upgradable",
        "name": "Upgradable",
        "manifest": {"description": "v2-broken", "env_keys": []},
        "files": [{"path": "scripts/cli", "content": "echo v2\n", "mode": "+x"}],
    }

    real_write = Path.write_text

    def flaky_write(self, *args, **kwargs):
        # Fail while writing the staging copy of upgradable's module.yaml.
        if self.name == "module.yaml":
            raise OSError("disk gremlin")
        return real_write(self, *args, **kwargs)

    import unittest.mock as mock

    with mock.patch.object(Path, "write_text", flaky_write), caplog.at_level(
        "WARNING"
    ):
        result = overlay_modules([v2], str(modules_dir), backoff_seconds=0.0)

    # The slug is reported failed, never installed.
    assert "upgradable" in result.failed
    assert "upgradable" not in result.installed

    # The OLD install survives intact — directory, manifest, and script.
    assert install_dir.is_dir()
    assert "v1-keep-me" in (install_dir / "module.yaml").read_text()
    assert script.is_file()
    assert script.read_text() == "echo v1\n"
    assert os.access(script, os.X_OK)
    assert (install_dir / "SKILL.md").read_text() == "# v1 skill\n"

    # No leftover staging directory pollutes the modules dir.
    leftovers = [
        p for p in modules_dir.iterdir() if p.name.startswith(".upgradable.tmp-")
    ]
    assert leftovers == [], f"leftover staging dirs: {leftovers}"

    # Structured failure record emitted for the slug.
    records = [r for r in caplog.records if MODULE_INSTALL_FAILED_EVENT in r.message]
    assert any("upgradable" in r.message for r in records)


def test_failed_final_swap_preserves_existing_install(tmp_path: Path, caplog):
    """An UPDATE whose staging writes all succeed but whose FINAL
    staging→install rename fails must NOT destroy the previously-working
    install.  Reproduces the swap-path bug: if the final ``os.replace`` of
    the staging dir into ``install_dir`` raises, the old v1 module must
    still be present and intact, the slug reported failed (not installed),
    and no staging/backup dirs left behind."""
    modules_dir = tmp_path / "modules"

    # Install a working v1 that must survive a failed swap.
    v1 = {
        "slug": "upgradable",
        "name": "Upgradable",
        "manifest": {"description": "v1-keep-me", "env_keys": ["KEEP_KEY"]},
        "files": [
            {"path": "scripts/cli", "content": "echo v1\n", "mode": "+x"},
            {"path": "SKILL.md", "content": "# v1 skill\n"},
        ],
    }
    overlay_modules([v1], str(modules_dir))
    install_dir = modules_dir / "upgradable"
    assert install_dir.is_dir()
    script = install_dir / "scripts" / "cli"
    assert script.is_file()

    # A v2 update whose writes all succeed but whose final swap blows up.
    v2 = {
        "slug": "upgradable",
        "name": "Upgradable",
        "manifest": {"description": "v2-broken", "env_keys": []},
        "files": [{"path": "scripts/cli", "content": "echo v2\n", "mode": "+x"}],
    }

    import os as _os
    import unittest.mock as mock

    real_replace = _os.replace
    install_dir_str = str(install_dir)
    staging_prefix = ".upgradable.tmp-"
    failed = {"count": 0}

    def flaky_replace(src, dst, *args, **kwargs):
        # Fail only on the final staging→install swap — i.e. a rename whose
        # *source* is the staging dir and whose *target* is install_dir,
        # after the old install has already been moved aside to its backup.
        # The subsequent backup→install restore (source = backup dir) must
        # still succeed so the previously-working module is recovered.
        if str(dst) == install_dir_str and Path(src).name.startswith(staging_prefix):
            failed["count"] += 1
            raise OSError("rename gremlin")
        return real_replace(src, dst, *args, **kwargs)

    with mock.patch.object(
        _os, "replace", flaky_replace
    ), caplog.at_level("WARNING"):
        result = overlay_modules([v2], str(modules_dir), backoff_seconds=0.0)

    # The injected fault actually fired on the final swap (guards the test
    # against silently never exercising the swap path).
    assert failed["count"] >= 1

    # The slug is reported failed, never installed.
    assert "upgradable" in result.failed
    assert "upgradable" not in result.installed

    # The OLD install survives intact — directory, manifest, and script.
    assert install_dir.is_dir()
    assert "v1-keep-me" in (install_dir / "module.yaml").read_text()
    assert script.is_file()
    assert script.read_text() == "echo v1\n"
    assert os.access(script, os.X_OK)
    assert (install_dir / "SKILL.md").read_text() == "# v1 skill\n"

    # No leftover staging OR backup directory pollutes the modules dir.
    leftovers = [
        p
        for p in modules_dir.iterdir()
        if p.name.startswith((".upgradable.tmp-", ".upgradable.bak-"))
    ]
    assert leftovers == [], f"leftover staging/backup dirs: {leftovers}"

    # Structured failure record emitted for the slug.
    records = [r for r in caplog.records if MODULE_INSTALL_FAILED_EVENT in r.message]
    assert any("upgradable" in r.message for r in records)


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


# ── Modules overlay decoupled from skills_dir ────────────────────────


def test_flag_on_no_skills_dir_still_overlays_modules(tmp_path: Path, monkeypatch):
    """Flag ON with ``skills_dir`` omitted → registry MODULES are still
    overlaid (materialised, scripts symlinked, doc rendered) and the result
    is NOT skipped.  Regression for: the overlay used to be gated on a
    skills dir, so a flag-on startup command without one did nothing."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"

    result = apply_registry_overlay(
        _resolved_payload(),
        modules_dir=str(modules_dir),
        # skills_dir intentionally omitted (None)
        bin_dir=str(bin_dir),
    )

    assert result.skipped is False
    assert result.fetch_failed is False
    # Module materialised even though no skills dir was supplied.
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()
    assert "registry-only-mod" in result.modules.installed
    # Its script is symlinked onto PATH + executable.
    link = bin_dir / "regmod-cli"
    assert link.is_symlink()
    assert os.access(link.resolve(), os.X_OK)
    # The skill from the payload was cleanly skipped, not written anywhere.
    assert "deep-research" in result.skills.skipped
    assert result.skills.written == []


def test_run_setup_flag_on_no_skills_dir_renders_module_doc(
    tmp_path: Path, monkeypatch
):
    """run_setup with the flag ON and ``skills_dir=None`` still overlays the
    registry module and renders it into the agent doc."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    agents_md = _agents_md(tmp_path)

    module_setup.run_setup(
        str(modules_dir),
        str(agents_md),
        bin_dir=str(bin_dir),
        registry_resolved=_resolved_payload(),
        skills_dir=None,
    )

    doc = agents_md.read_text()
    assert "registry-only-mod" in doc  # registry module documented
    assert "superpos-github" in doc  # baked-in still present
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()


def test_flag_on_no_skills_dir_does_not_raise_on_skills(tmp_path: Path, monkeypatch):
    """A payload carrying skills overlaid with no skills_dir must skip the
    skills portion cleanly (no exception) while still installing modules."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"

    # Should not raise even though there are skills to write and no dir.
    result = apply_registry_overlay(
        {"skills": _resolved_payload()["skills"], "modules": _resolved_payload()["modules"]},
        modules_dir=str(modules_dir),
        skills_dir=None,
    )

    assert result.skipped is False
    assert "registry-only-mod" in result.modules.installed
    assert "deep-research" in result.skills.skipped


def test_cli_main_fetches_resolved_when_flag_on_without_skills_dir(
    tmp_path: Path, monkeypatch
):
    """CLI ``main()`` must fetch the resolved set whenever the flag is ON,
    even when ``--skills-dir`` is not passed.  Regression for the reviewer's
    finding that the CLI never fetched without ``--skills-dir``."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    agents_md = _agents_md(tmp_path)

    called = {"fetch": 0}

    def _fake_fetch(*args, **kwargs):
        called["fetch"] += 1
        return _resolved_payload()

    monkeypatch.setattr(module_setup, "_fetch_registry_resolved", _fake_fetch)
    monkeypatch.setattr(
        "sys.argv",
        [
            "module_setup",
            "--modules-dir", str(modules_dir),
            "--agents-md", str(agents_md),
            # NOTE: no --skills-dir
        ],
    )

    module_setup.main()

    assert called["fetch"] == 1, "CLI must fetch the resolved set with flag on"
    # And the module was actually overlaid.
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()


def test_cli_main_does_not_fetch_when_flag_off(tmp_path: Path, monkeypatch):
    """Flag explicitly OFF → CLI ``main()`` makes zero registry fetches
    regardless of whether ``--skills-dir`` is passed."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "false")
    modules_dir = tmp_path / "modules"
    agents_md = _agents_md(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("must not fetch when flag off")

    monkeypatch.setattr(module_setup, "_fetch_registry_resolved", _boom)
    monkeypatch.setattr(
        "sys.argv",
        [
            "module_setup",
            "--modules-dir", str(modules_dir),
            "--agents-md", str(agents_md),
        ],
    )

    module_setup.main()  # must not raise

    # No registry module installed.
    assert not (modules_dir / "registry-only-mod").exists()


# ── Instant-rollback: flag-ON install then flag-OFF restart ──────────


def test_flag_on_install_then_flag_off_restart_removes_registry_module(
    tmp_path: Path, monkeypatch
):
    """Reviewer regression (PR #29): a registry-only module materialised
    during a flag-ON run must be GONE — absent from docs AND PATH — after a
    flag-OFF restart against the *same* modules/bin dirs.  This is the
    instant-rollback guarantee that was previously broken because the
    baked-in discover/symlink/doc path picked up the persisted dir before
    the flag was ever checked."""
    modules_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    skills_dir = tmp_path / "skills"
    agents_md = _agents_md(tmp_path)

    # 1) Flag ON — install the registry-only module.
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    module_setup.run_setup(
        str(modules_dir),
        str(agents_md),
        bin_dir=str(bin_dir),
        registry_resolved=_resolved_payload(),
        skills_dir=str(skills_dir),
    )
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()
    assert (bin_dir / "regmod-cli").is_symlink()
    assert "registry-only-mod" in agents_md.read_text()

    # 2) Flag OFF — restart against the SAME dirs.  No payload (the CLI never
    #    even fetches when the flag is off).  Explicit false = the rollback.
    monkeypatch.setenv(FEATURE_FLAG_ENV, "false")
    module_setup.run_setup(
        str(modules_dir),
        str(agents_md),
        bin_dir=str(bin_dir),
        registry_resolved=None,
        skills_dir=str(skills_dir),
    )

    # The registry-only module is fully rolled back: gone from disk, gone
    # from PATH, gone from the rendered docs.
    assert not (modules_dir / "registry-only-mod").exists()
    assert not (bin_dir / "regmod-cli").exists()  # symlink unlinked, not dangling
    assert not (bin_dir / "regmod-cli").is_symlink()
    doc = agents_md.read_text()
    assert "registry-only-mod" not in doc
    # Baked-in modules are unaffected by the rollback.
    assert "superpos-github" in doc


def test_flag_off_rollback_preserves_bundled_and_hand_authored_modules(
    tmp_path: Path, monkeypatch
):
    """The rollback sweep removes ONLY registry-managed dirs (those carrying
    the marker) — a hand-authored workspace module without the marker is
    left untouched."""
    from superpos_agent_core.registry_overlay import REGISTRY_MANAGED_MARKER

    modules_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    agents_md = _agents_md(tmp_path)

    # A hand-authored workspace module (no marker).
    hand = modules_dir / "hand-authored"
    (hand / "scripts").mkdir(parents=True)
    (hand / "module.yaml").write_text("description: hand authored\nenv: []\n")
    (hand / "scripts" / "hand-cli").write_text("#!/bin/sh\necho hi\n")

    # A registry-managed module (carries the marker).
    reg = modules_dir / "reg-mod"
    (reg / "scripts").mkdir(parents=True)
    (reg / "module.yaml").write_text("description: reg\nenv: []\n")
    (reg / "scripts" / "reg-cli").write_text("#!/bin/sh\necho reg\n")
    (reg / REGISTRY_MANAGED_MARKER).write_text("")

    monkeypatch.setenv(FEATURE_FLAG_ENV, "false")
    module_setup.run_setup(
        str(modules_dir), str(agents_md), bin_dir=str(bin_dir),
    )

    assert (modules_dir / "hand-authored").is_dir()  # untouched
    assert not (modules_dir / "reg-mod").exists()  # rolled back
    doc = agents_md.read_text()
    assert "hand-authored" in doc
    assert "reg-mod" not in doc


def test_remove_registry_overlay_modules_unlinks_only_matching_bin_links(
    tmp_path: Path,
):
    """The bin cleanup unlinks the removed module's symlinks (matched by
    target, even once broken) and leaves an unrelated same-named link of a
    different target alone is not required here — but a link pointing into
    the removed module must go, and links into a surviving module stay."""
    from superpos_agent_core.registry_overlay import (
        REGISTRY_MANAGED_MARKER,
        remove_registry_overlay_modules,
    )

    modules_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    reg = modules_dir / "reg-mod"
    (reg / "scripts").mkdir(parents=True)
    script = reg / "scripts" / "reg-cli"
    script.write_text("#!/bin/sh\n")
    (reg / REGISTRY_MANAGED_MARKER).write_text("")
    (bin_dir / "reg-cli").symlink_to(script.resolve())

    # A surviving (non-registry) module + its bin link must remain.
    keep_mod = modules_dir / "keep-mod"
    (keep_mod / "scripts").mkdir(parents=True)
    keep_script = keep_mod / "scripts" / "keep-cli"
    keep_script.write_text("#!/bin/sh\n")
    (bin_dir / "keep-cli").symlink_to(keep_script.resolve())

    removed = remove_registry_overlay_modules(str(modules_dir), bin_dir=str(bin_dir))

    assert removed == ["reg-mod"]
    assert not (modules_dir / "reg-mod").exists()
    assert not (bin_dir / "reg-cli").is_symlink()  # stale link cleared
    assert (modules_dir / "keep-mod").is_dir()  # unrelated module kept
    assert (bin_dir / "keep-cli").is_symlink()  # unrelated link kept


# ── Flag-ON reconcile: module disappears from the resolved set ───────


def test_flag_on_reconcile_removes_module_absent_from_resolved(
    tmp_path: Path, monkeypatch
):
    """When the flag stays ON across restarts but a registry module
    disappears from ``/registry/resolved`` (removed / unauthorised), the
    next overlay must drop it from disk, PATH and docs — while keeping the
    modules the registry still serves."""
    modules_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    skills_dir = tmp_path / "skills"
    agents_md = _agents_md(tmp_path)
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")

    # Round 1 — registry serves the module.
    module_setup.run_setup(
        str(modules_dir), str(agents_md), bin_dir=str(bin_dir),
        registry_resolved=_resolved_payload(), skills_dir=str(skills_dir),
    )
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()
    assert (bin_dir / "regmod-cli").is_symlink()

    # Round 2 — registry no longer serves it (empty module set).
    module_setup.run_setup(
        str(modules_dir), str(agents_md), bin_dir=str(bin_dir),
        registry_resolved={"skills": [], "modules": []},
        skills_dir=str(skills_dir),
    )
    assert not (modules_dir / "registry-only-mod").exists()
    assert not (bin_dir / "regmod-cli").is_symlink()
    assert "registry-only-mod" not in agents_md.read_text()


def test_flag_on_fetch_failure_does_not_reconcile_away_existing_module(
    tmp_path: Path, monkeypatch
):
    """A transient fetch failure (resolved is None) must NOT delete a
    previously-installed registry module — the reconcile only runs against a
    fresh, authoritative resolved set."""
    modules_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    skills_dir = tmp_path / "skills"
    agents_md = _agents_md(tmp_path)
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")

    module_setup.run_setup(
        str(modules_dir), str(agents_md), bin_dir=str(bin_dir),
        registry_resolved=_resolved_payload(), skills_dir=str(skills_dir),
    )
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()

    # Flag still ON but fetch failed → resolved is None → fall back to
    # baked-in, leave the existing registry module in place.
    module_setup.run_setup(
        str(modules_dir), str(agents_md), bin_dir=str(bin_dir),
        registry_resolved=None, skills_dir=str(skills_dir),
    )
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()
    assert (bin_dir / "regmod-cli").is_symlink()


# ── Beat 4 prereq: resilient fetch (cache + retry + degraded-empty) ───


def _fetch_env(monkeypatch):
    """Set the env the resilient fetch needs to attempt a live GET."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    monkeypatch.setenv("SUPERPOS_BASE_URL", "https://example.test")
    monkeypatch.setenv("SUPERPOS_API_TOKEN", "tok")


def test_successful_fetch_writes_cache_then_failure_loads_it(
    tmp_path: Path, monkeypatch
):
    """A 200 fetch persists the last-good cache; a later all-fail fetch loads
    + returns it (tagged ``_from_cache``) instead of None."""
    _fetch_env(monkeypatch)
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    cache = tmp_path / "cache.json"
    monkeypatch.setenv(module_setup.REGISTRY_RESOLVED_CACHE_ENV, str(cache))

    payload = _resolved_payload()

    # Live fetch succeeds → cache written, payload returned verbatim.
    monkeypatch.setattr(
        module_setup, "_live_fetch_registry_resolved",
        lambda base, tok: payload,
    )
    got = module_setup._fetch_registry_resolved(str(modules_dir))
    assert got == payload
    assert cache.is_file()

    # Now the live fetch fails on every attempt → cache is loaded.
    monkeypatch.setattr(
        module_setup, "_live_fetch_registry_resolved",
        lambda base, tok: None,
    )
    cached = module_setup._fetch_registry_resolved(
        str(modules_dir), retries=0, backoff_seconds=0,
    )
    assert cached is not None
    assert cached["_from_cache"] is True
    assert cached["modules"][0]["slug"] == "registry-only-mod"


def test_cached_payload_drives_overlay(tmp_path: Path, monkeypatch):
    """The cache-sourced payload overlays exactly like a live one."""
    _fetch_env(monkeypatch)
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    skills_dir = tmp_path / "skills"
    cache = tmp_path / "cache.json"
    monkeypatch.setenv(module_setup.REGISTRY_RESOLVED_CACHE_ENV, str(cache))

    cache.write_text(__import__("json").dumps(_resolved_payload()))
    monkeypatch.setattr(
        module_setup, "_live_fetch_registry_resolved",
        lambda base, tok: None,
    )
    resolved = module_setup._fetch_registry_resolved(
        str(modules_dir), retries=0, backoff_seconds=0,
    )
    result = apply_registry_overlay(
        resolved, modules_dir=str(modules_dir), skills_dir=str(skills_dir),
    )
    assert not result.fetch_failed
    assert not result.degraded_empty
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()
    assert (skills_dir / "deep-research.md").is_file()


def test_fetch_failure_no_cache_returns_none(tmp_path: Path, monkeypatch):
    """Live fetch fails AND no cache → returns None (caller degrades)."""
    _fetch_env(monkeypatch)
    cache = tmp_path / "missing.json"
    monkeypatch.setenv(module_setup.REGISTRY_RESOLVED_CACHE_ENV, str(cache))
    monkeypatch.setattr(
        module_setup, "_live_fetch_registry_resolved",
        lambda base, tok: None,
    )
    got = module_setup._fetch_registry_resolved(
        str(tmp_path / "modules"), retries=0, backoff_seconds=0,
    )
    assert got is None
    assert not cache.exists()


def test_retry_transient_then_success_within_budget(tmp_path: Path, monkeypatch):
    """Transient failures then a success → succeeds inside the retry budget,
    and never sleeps longer than the bounded backoff schedule."""
    _fetch_env(monkeypatch)
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    monkeypatch.setenv(
        module_setup.REGISTRY_RESOLVED_CACHE_ENV, str(tmp_path / "c.json")
    )

    calls = {"n": 0}

    def _flaky(base, tok):
        calls["n"] += 1
        return _resolved_payload() if calls["n"] >= 3 else None

    slept: list[float] = []
    monkeypatch.setattr(module_setup, "_live_fetch_registry_resolved", _flaky)
    got = module_setup._fetch_registry_resolved(
        str(modules_dir), retries=2, backoff_seconds=0.01, sleep=slept.append,
    )
    assert got is not None and "_from_cache" not in got
    assert calls["n"] == 3
    # 1.0,2.0 style schedule with base 0.01 → 0.01 + 0.02
    assert len(slept) == 2


def test_retry_all_fail_falls_to_cache(tmp_path: Path, monkeypatch):
    """All attempts fail → falls back to the last-good cache."""
    _fetch_env(monkeypatch)
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    cache = tmp_path / "c.json"
    monkeypatch.setenv(module_setup.REGISTRY_RESOLVED_CACHE_ENV, str(cache))
    cache.write_text(__import__("json").dumps(_resolved_payload()))

    monkeypatch.setattr(
        module_setup, "_live_fetch_registry_resolved", lambda base, tok: None
    )
    slept: list[float] = []
    got = module_setup._fetch_registry_resolved(
        str(modules_dir), retries=2, backoff_seconds=0.01, sleep=slept.append,
    )
    assert got is not None and got["_from_cache"] is True
    assert len(slept) == 2  # retried before giving up


def test_degraded_empty_boots_and_logs(tmp_path: Path, monkeypatch, caplog):
    """Fetch failed + no cache + nothing baked-in → degraded-empty result,
    the agent still 'boots' (no raise), and the structured event is logged."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    skills_dir = tmp_path / "skills"

    # Simulate the Beat 4 lean image: no baked-in modules discoverable.
    import superpos_agent_core.registry_overlay as ro
    monkeypatch.setattr(ro, "discover_modules", lambda *a, **k: [])

    with caplog.at_level("WARNING"):
        result = apply_registry_overlay(
            None, modules_dir=str(modules_dir), skills_dir=str(skills_dir),
        )

    assert result.fetch_failed is True
    assert result.degraded_empty is True
    assert any(
        RESOLVED_EMPTY_NO_FALLBACK_EVENT in r.message for r in caplog.records
    )


def test_degraded_empty_when_baked_in_present_is_benign(
    tmp_path: Path, monkeypatch
):
    """Fetch failed + no cache but baked-in modules exist → benign degrade,
    NOT degraded-empty."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    # Real package bundled modules are discoverable → has_baked_in is True.
    result = apply_registry_overlay(
        None, modules_dir=str(tmp_path / "modules"),
        skills_dir=str(tmp_path / "skills"),
    )
    assert result.fetch_failed is True
    assert result.degraded_empty is False


def test_run_setup_degraded_empty_does_not_raise(tmp_path: Path, monkeypatch):
    """A degraded-empty boot must never crash run_setup."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    import superpos_agent_core.registry_overlay as ro
    monkeypatch.setattr(ro, "discover_modules", lambda *a, **k: [])
    # Should simply return.
    module_setup.run_setup(
        str(tmp_path / "modules"),
        str(_agents_md(tmp_path)),
        registry_resolved=None,
        skills_dir=str(tmp_path / "skills"),
    )


def test_failed_fetch_does_not_purge_installed_registry_module(
    tmp_path: Path, monkeypatch
):
    """Reconcile safety: a fetch failure (resolved=None) short-circuits the
    destructive reconcile, so an already-installed registry module survives."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "true")
    modules_dir = tmp_path / "modules"
    bin_dir = tmp_path / "bin"
    skills_dir = tmp_path / "skills"
    agents_md = _agents_md(tmp_path)

    # First install a registry module from a good payload.
    apply_registry_overlay(
        _resolved_payload(), modules_dir=str(modules_dir),
        skills_dir=str(skills_dir), agents_md_path=str(agents_md),
        bin_dir=str(bin_dir),
    )
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()

    # Now a failed fetch → must NOT remove the installed registry module.
    result = apply_registry_overlay(
        None, modules_dir=str(modules_dir), skills_dir=str(skills_dir),
        agents_md_path=str(agents_md), bin_dir=str(bin_dir),
    )
    assert result.fetch_failed is True
    assert (modules_dir / "registry-only-mod" / "module.yaml").is_file()


def test_flag_off_never_fetches_or_touches_cache(tmp_path: Path, monkeypatch):
    """Flag OFF → main() makes zero fetches and never writes the cache."""
    monkeypatch.setenv(FEATURE_FLAG_ENV, "false")
    modules_dir = tmp_path / "modules"
    cache = tmp_path / "cache.json"
    monkeypatch.setenv(module_setup.REGISTRY_RESOLVED_CACHE_ENV, str(cache))

    def _boom(base, tok):
        raise AssertionError("must not live-fetch when flag off")

    monkeypatch.setattr(module_setup, "_live_fetch_registry_resolved", _boom)
    monkeypatch.setattr(
        "sys.argv",
        [
            "module_setup",
            "--modules-dir", str(modules_dir),
            "--agents-md", str(_agents_md(tmp_path)),
        ],
    )
    module_setup.main()
    assert not cache.exists()


def test_cache_write_is_atomic_via_replace(tmp_path: Path, monkeypatch):
    """The cache write must go through a temp file + os.replace (atomic)."""
    import os as _os

    replaced: list[tuple] = []
    real_replace = _os.replace

    def _spy_replace(src, dst):
        replaced.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(module_setup.os, "replace", _spy_replace)
    cache = tmp_path / "out" / "cache.json"
    monkeypatch.setenv(module_setup.REGISTRY_RESOLVED_CACHE_ENV, str(cache))

    module_setup._write_registry_resolved_cache(_resolved_payload(), None)
    assert cache.is_file()
    assert replaced and Path(replaced[-1][1]) == cache
    # The temp source must not linger.
    assert not Path(replaced[-1][0]).exists()
