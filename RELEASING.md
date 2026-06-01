# Releasing `superpos-agent-core`

This package is published to **PyPI** so the agent repos
(`superpos-agent-claude`, `-codex`, `-gemini`, `-qwen`) can depend on a
**version range** instead of a pinned git commit. That means a core change
flows to every agent on their next image rebuild — no per-repo bump PR.

## How agents depend on it

Each agent pins a compatible range, not a commit:

```
superpos-agent-core~=0.1   # >=0.1,<1.0 — any 0.x release
```

While on `0.x`, **minor bumps (0.1 → 0.2) are treated as non-breaking** and
flow to agents automatically. Use a **major bump to `1.0.0`** (and tighten
agent pins to `~=1.0`) as the hard "opt-in / breaking change" wall. If you
ever want a specific core change to NOT auto-flow, that's a major bump.

## Cutting a release

1. Bump `project.version` in `pyproject.toml` (e.g. `0.1.0` → `0.2.0`).
2. Merge that to `main`.
3. Tag and push — the tag **must** be `v` + the pyproject version:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
4. `.github/workflows/release.yml` builds the sdist+wheel, verifies the tag
   matches the version, checks the bundled modules shipped, and publishes to
   PyPI via Trusted Publishing (no token).

`workflow_dispatch` builds the distributions as a dry-run (the publish job is
gated to real tag pushes only).

## One-time PyPI setup (do this once, before the first tag)

Trusted Publishing lets GitHub Actions authenticate to PyPI over OIDC with no
stored secret. On <https://pypi.org>:

1. Create the project owner / log in.
2. For the **first** release only, PyPI needs the project to exist or a
   "pending" trusted publisher. Go to your account →
   **Publishing** → **Add a pending publisher** and enter:
   - **PyPI Project Name:** `superpos-agent-core`
   - **Owner:** `Superpos-AI`
   - **Repository name:** `superpos-agent-core`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
3. (After the project exists, manage this under the project's
   **Settings → Publishing** instead.)
4. In this GitHub repo, create an **Environment** named `pypi`
   (Settings → Environments) — optionally add required reviewers to gate
   releases behind a manual approval.

Once the pending publisher is configured, pushing `v0.1.0` publishes the first
release with zero secrets in the repo.
