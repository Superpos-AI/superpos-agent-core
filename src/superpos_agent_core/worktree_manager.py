"""Git worktree management for per-branch agent isolation."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def is_git_repo(path: str) -> bool:
    """Return True if path contains a git repository."""
    return Path(path, ".git").exists()


def _safe_branch_name(branch: str) -> str:
    return branch.replace("/", "-").replace(" ", "-")


def worktree_path(base: str, branch: str) -> str:
    """Return the filesystem path for a branch's worktree."""
    return os.path.join(base, ".worktrees", _safe_branch_name(branch))


def slot_key(base: str, branch: str | None) -> str:
    """Return the worktree slot key for serialization. Same key = same lock."""
    if branch:
        return worktree_path(base, branch)
    return "__main__"


def infer_branch(task: dict) -> str | None:
    """Extract branch name from a Superpos task's event payload.

    Priority:
    1. event_payload.pull_request.head.ref  (PR events)
    2. event_payload.ref → strip refs/heads/ prefix  (push events)
    3. payload.branch or invoke.branch  (explicit override)
    """
    payload = task.get("payload", {}) or {}
    invoke = task.get("invoke", {}) or {}

    event_payload = task.get("event_payload") or (
        payload.get("event_payload") if isinstance(payload, dict) else None
    )

    if isinstance(event_payload, dict):
        bodies = [event_payload]
        body = event_payload.get("body")
        if isinstance(body, dict):
            bodies.append(body)

        for ev in bodies:
            pr = ev.get("pull_request") or {}
            if isinstance(pr, dict):
                head = pr.get("head") or {}
                if isinstance(head, dict):
                    ref = head.get("ref")
                    if ref:
                        return ref

        for ev in bodies:
            ref = ev.get("ref", "")
            if ref and ref.startswith("refs/heads/"):
                return ref[len("refs/heads/"):]

    if isinstance(payload, dict):
        branch = payload.get("branch")
        if branch:
            return branch
    if isinstance(invoke, dict):
        branch = invoke.get("branch")
        if branch:
            return branch

    return None


async def _fetch_origin(base: str) -> None:
    """Fetch latest refs from origin so worktrees start from up-to-date state."""
    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", base, "fetch", "origin"],
        capture_output=True,
        text=True,
        timeout=60,
    )


async def ensure_worktree(base: str, branch: str) -> str:
    """Create a worktree for *branch* if one does not already exist.

    Returns the worktree directory path.
    """
    path = worktree_path(base, branch)

    if os.path.isdir(path):
        log.debug("Reusing existing worktree for branch %r at %s", branch, path)
        return path

    os.makedirs(os.path.join(base, ".worktrees"), exist_ok=True)
    await _fetch_origin(base)

    log.info("Creating worktree for branch %r at %s", branch, path)

    result = await asyncio.to_thread(
        subprocess.run,
        [
            "git", "-C", base, "worktree", "add",
            "--track", "-b", branch, path, f"origin/{branch}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return path

    result2 = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", base, "worktree", "add", path, branch],
        capture_output=True,
        text=True,
    )
    if result2.returncode == 0:
        return path

    log.info("Branch %r not found on origin or locally; creating from origin/main", branch)
    result3 = await asyncio.to_thread(
        subprocess.run,
        [
            "git", "-C", base, "worktree", "add",
            "-b", branch, path, "origin/main",
        ],
        capture_output=True,
        text=True,
    )
    if result3.returncode == 0:
        return path

    raise RuntimeError(
        f"git worktree add failed for branch {branch!r}: {result3.stderr.strip()}"
    )


async def prune_worktrees(base: str) -> None:
    """Run git worktree prune to remove stale worktree metadata."""
    result = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", base, "worktree", "prune"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.warning("git worktree prune failed: %s", result.stderr.strip())
    else:
        log.info("git worktree prune completed")
