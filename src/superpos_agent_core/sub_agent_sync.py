"""Sync Superpos SubAgentDefinitions → local subagent files.

Fetches active sub-agent definitions from the Superpos API, writes each as a
subagent ``.md`` file (YAML frontmatter + markdown body), and removes stale
managed files that no longer exist on the platform.

Optionally injects the agent's persona MEMORY and a summary of installed
modules/skills so that subagents inherit the parent agent's learned context
and available tooling.

Run at container startup from entrypoint.sh, or on-demand:

    python3 -m superpos_agent_core.sub_agent_sync
    python3 -m superpos_agent_core.sub_agent_sync --inject-memory --inject-modules
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Callable

import httpx
import yaml

from .persona_overlay import MemoryFetchUnavailable, read_memory

log = logging.getLogger(__name__)

MANAGED_MARKER = "<!-- managed-by: superpos-sync -->"
DOCUMENT_ORDER = ("SOUL", "AGENT", "RULES", "STYLE", "EXAMPLES", "NOTES")

# Sentinel for the ``memory`` argument so we can tell "caller omitted memory"
# (no authoritative value — must NOT clear the snapshot) apart from "caller
# explicitly passed an empty/cleared MEMORY" (authoritative reachable-empty).
_MEMORY_OMITTED = object()


class SubAgentFetchError(RuntimeError):
    """Raised when sub-agent definitions cannot be reliably fetched.

    Distinguishes transport / API failures from a legitimate empty
    response so callers don't treat a failed fetch as "platform has zero
    sub-agents" and delete managed files.
    """


# ── HTTP helpers (sync, for CLI usage) ────────────────────────────────


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def fetch_runtime_bundle(
    base_url: str, token: str,
) -> dict | None:
    """Fetch all definitions + agent memory in one call via runtime-bundle endpoint.

    Returns dict with 'definitions', 'agent_memory', 'persona_version' or None
    if the endpoint is not available (older backend).
    """
    try:
        with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
            resp = client.get("/api/v1/sub-agents/runtime-bundle", headers=_headers(token))
            if resp.status_code != 200:
                return None
            data = resp.json()
            payload = data.get("data", data) if isinstance(data, dict) else {}
            if not isinstance(payload, dict) or "definitions" not in payload:
                return None
            return payload
    except Exception:
        return None


def fetch_sub_agent_definitions(
    base_url: str, token: str,
) -> list[dict]:
    """Fetch all active sub-agent definitions with full documents (N+1 fallback).

    Raises:
        SubAgentFetchError: if the list endpoint fails (non-200) or returns
            an unparseable body, or if any per-slug detail fetch fails.
            Callers must distinguish this from a legitimate empty list.
    """
    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        try:
            resp = client.get("/api/v1/sub-agents", headers=_headers(token))
        except httpx.HTTPError as exc:
            raise SubAgentFetchError(f"list request failed: {exc}") from exc

        if resp.status_code != 200:
            log.warning("Failed to list sub-agents: %s %s", resp.status_code, resp.text[:200])
            raise SubAgentFetchError(
                f"list endpoint returned {resp.status_code}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise SubAgentFetchError(f"list response not JSON: {exc}") from exc
        summaries = data.get("data", data) if isinstance(data, dict) else []
        if not isinstance(summaries, list):
            raise SubAgentFetchError("list response payload is not a list")

        definitions = []
        for summary in summaries:
            slug = summary.get("slug")
            if not slug:
                continue
            try:
                detail_resp = client.get(
                    f"/api/v1/sub-agents/{slug}", headers=_headers(token),
                )
            except httpx.HTTPError as exc:
                raise SubAgentFetchError(
                    f"detail request for {slug} failed: {exc}"
                ) from exc
            if detail_resp.status_code != 200:
                log.warning(
                    "Failed to fetch sub-agent %s: %s", slug, detail_resp.status_code,
                )
                raise SubAgentFetchError(
                    f"detail endpoint for {slug} returned {detail_resp.status_code}"
                )
            try:
                detail_data = detail_resp.json()
            except ValueError as exc:
                raise SubAgentFetchError(
                    f"detail response for {slug} not JSON: {exc}"
                ) from exc
            definition = detail_data.get("data", detail_data)
            if not isinstance(definition, dict):
                raise SubAgentFetchError(
                    f"detail payload for {slug} is not an object"
                )
            definitions.append(definition)

        return definitions


def fetch_persona_memory(base_url: str, token: str) -> str | None:
    """Fetch the MEMORY document from the active persona.

    Distinguishes a *reachable* read from an *outage* so the AG-10 overlay can
    tell "the user cleared MEMORY" (→ stop injecting) from "Superpos is down"
    (→ serve the snapshot):

    - **Reachable** (HTTP 200) → return the document content, or ``None`` when
      the document is empty / blank.  ``None`` here means *reachable-empty*.
    - **Reachable-empty** (HTTP 404) → return ``None``.  The server's
      ``PersonaController::document()`` returns ``notFound()`` for both "no
      active persona for this agent" and "MEMORY document missing", so a 404 is
      a reachable cleared state — NOT an outage.  Mirrors
      :meth:`SuperposClient.get_persona_assembled`.
    - **Outage** (transport error, or any other non-200 status) → raise
      :class:`MemoryFetchUnavailable` so callers fall back to the snapshot.
    """
    try:
        with httpx.Client(
            base_url=base_url, timeout=30.0, follow_redirects=True,
        ) as client:
            resp = client.get(
                "/api/v1/persona/documents/MEMORY", headers=_headers(token),
            )
    except httpx.HTTPError as exc:
        raise MemoryFetchUnavailable(
            f"MEMORY fetch transport error: {exc}"
        ) from exc
    # 404 is the server's reachable "no active persona / no MEMORY document"
    # state, NOT an outage — return reachable-empty so callers clear the stale
    # snapshot instead of resurrecting it.
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise MemoryFetchUnavailable(
            f"MEMORY endpoint returned {resp.status_code}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise MemoryFetchUnavailable(
            f"MEMORY response not JSON: {exc}"
        ) from exc
    payload = data.get("data", data) if isinstance(data, dict) else {}
    if isinstance(payload, dict):
        return _get_document_content(payload.get("content"))
    return None


# ── Document helpers ──────────────────────────────────────────────────


def _get_document_content(doc_value: str | dict | None) -> str | None:
    """Extract content string from a document value (handles both string and object formats)."""
    if doc_value is None:
        return None
    if isinstance(doc_value, str):
        return doc_value if doc_value.strip() else None
    if isinstance(doc_value, dict):
        content = doc_value.get("content", "")
        return content if isinstance(content, str) and content.strip() else None
    return None


def assemble_prompt(documents: dict) -> str:
    """Assemble documents into a prompt, matching server-side logic."""
    parts = []
    for doc_name in DOCUMENT_ORDER:
        content = _get_document_content(documents.get(doc_name))
        if content:
            parts.append(f"# {doc_name}\n\n{content}")
    return "\n\n".join(parts)


# ── Local context discovery ───────────────────────────────────────────


def discover_local_context(
    modules_dir: str | None,
    skills_dir: str | None,
    subagent_slugs: list[str],
) -> str:
    """Build a context summary of available modules, skills, and sibling subagents."""
    sections = []

    if modules_dir and Path(modules_dir).is_dir():
        module_names = []
        for entry in sorted(Path(modules_dir).iterdir()):
            if entry.is_dir() and (entry / "module.yaml").exists():
                module_names.append(entry.name)
        if module_names:
            lines = ["**Installed modules** (available on PATH):"]
            for name in module_names:
                lines.append(f"- `{name}`")
            sections.append("\n".join(lines))

    if skills_dir and Path(skills_dir).is_dir():
        skill_names = []
        for entry in sorted(Path(skills_dir).iterdir()):
            if entry.suffix == ".md":
                skill_names.append(entry.stem)
        if skill_names:
            lines = ["**Available skills** (invoke with `/skill-name`):"]
            for name in skill_names:
                lines.append(f"- `/{name}`")
            sections.append("\n".join(lines))

    if subagent_slugs:
        lines = ['**Sibling subagents** (delegate with `Agent(subagent_type="name")`):']
        for slug in sorted(subagent_slugs):
            lines.append(f"- `{slug}`")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ── File generation ───────────────────────────────────────────────────


def build_subagent_md(
    definition: dict,
    *,
    memory: str | None = None,
    local_context: str | None = None,
) -> str:
    """Build the content of a subagent .md file (YAML frontmatter + markdown body)."""
    slug = definition["slug"]
    name = definition.get("name", slug)
    description = definition.get("description", "")
    model = definition.get("model") or None
    config = definition.get("config") or {}
    allowed_tools = definition.get("allowed_tools")
    version = definition.get("version", 1)
    documents = definition.get("documents") or {}

    if not model and isinstance(config, dict):
        llm_config = config.get("llm", {})
        if isinstance(llm_config, dict):
            model = llm_config.get("model")

    desc_text = f"{name} — {description}" if description else name

    # Serialize via yaml.safe_dump so quotes/newlines/colons in name or
    # description don't produce broken frontmatter that yaml.safe_load
    # can't round-trip.
    frontmatter_data: dict[str, object] = {
        "name": slug,
        "description": desc_text,
    }
    if model:
        frontmatter_data["model"] = model

    frontmatter_yaml = yaml.safe_dump(
        frontmatter_data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    ).rstrip()

    frontmatter = (
        "---\n"
        f"{frontmatter_yaml}\n"
        f"# synced from Superpos SubAgentDefinition v{version}\n"
        "---"
    )

    body = assemble_prompt(documents)

    parts = [frontmatter, ""]

    if body:
        parts.append(body)

    if allowed_tools:
        tools_str = ", ".join(f"`{t}`" for t in allowed_tools)
        parts.append(f"## Allowed Tools\n\nYou may only use these tools: {tools_str}")

    if memory and memory.strip():
        parts.append(
            "## Agent Memory\n\n"
            "The following is context the parent agent has learned. "
            "Use it to inform your work:\n\n"
            + memory.strip()
        )

    if local_context and local_context.strip():
        parts.append(f"## Agent Capabilities\n\n{local_context.strip()}")

    parts.append(MANAGED_MARKER)
    parts.append("")

    return "\n\n".join(parts)


# ── Main sync logic ──────────────────────────────────────────────────


def sync_sub_agents(
    subagents_dir: str,
    base_url: str,
    token: str,
    *,
    inject_memory: bool = False,
    modules_dir: str | None = None,
    skills_dir: str | None = None,
    definitions: list[dict] | None = None,
    memory: str | None = _MEMORY_OMITTED,  # type: ignore[assignment]
    memory_snapshot_dir: str | None = None,
) -> int:
    """Sync SubAgentDefinitions from Superpos to the subagents directory.

    If ``definitions`` and ``memory`` are provided, they are used directly
    (useful when the caller already has the data, e.g. from an async
    ``SuperposClient.get_runtime_bundle()`` call).  Otherwise, fetches
    from the API using sync HTTP.

    ``memory`` distinguishes *omitted* (default ``_MEMORY_OMITTED`` — no
    authoritative value, e.g. the caller passed ``definitions`` directly but no
    memory) from an *explicit* value (``str``/``None``, an authoritative
    reachable read).  An omitted memory must NOT be treated as a reachable-empty
    document — that would clear the workspace snapshot and lose the last-known-
    good MEMORY fallback for a later outage.

    When ``memory_snapshot_dir`` is provided (and ``inject_memory`` is set),
    the MEMORY read is routed through the AG-10 snapshot overlay: a Superpos
    outage degrades to the read-only workspace snapshot instead of dropping
    MEMORY injection, and a reachable read re-syncs that snapshot.

    Returns the number of definitions synced.
    """
    Path(subagents_dir).mkdir(parents=True, exist_ok=True)

    # MEMORY read for the doubling overlay.  ``fetch_fn`` must distinguish a
    # *reachable-empty* document (returns ``None``/blank → clears the snapshot,
    # no injection) from an *outage* (raises ``MemoryFetchUnavailable`` → serve
    # the snapshot).  Default: if ``memory`` was *omitted* there is no
    # authoritative value to act on, so treat it like an outage (raise) and
    # preserve the snapshot; only an *explicit* ``memory`` is an authoritative
    # reachable read.  The branches below replace this with the live source.
    def _default_memory_fetch() -> str | None:
        if memory is _MEMORY_OMITTED:
            raise MemoryFetchUnavailable(
                "no authoritative MEMORY available (memory omitted)"
            )
        return memory  # type: ignore[return-value]

    memory_fetch_fn: Callable[[], str | None] = _default_memory_fetch

    if definitions is None:
        bundle = fetch_runtime_bundle(base_url, token)

        if bundle is not None:
            definitions = bundle.get("definitions") or []
            if inject_memory:
                # The bundle was reachable, so its memory value (possibly empty)
                # is authoritative — never an outage.
                memory = bundle.get("agent_memory")
                memory_fetch_fn = lambda: bundle.get("agent_memory")  # noqa: E731
            log.info(
                "Fetched runtime bundle: %d definition(s), memory=%s",
                len(definitions),
                "yes" if (memory is not _MEMORY_OMITTED and memory) else "no",
            )
        else:
            try:
                definitions = fetch_sub_agent_definitions(base_url, token)
            except SubAgentFetchError as exc:
                # Bail out without touching any managed files — we cannot
                # distinguish "no sub-agents" from "fetch failed", and
                # deleting on failure would wipe valid local state.
                log.warning(
                    "Skipping sub-agent sync: fetch failed (%s); "
                    "leaving existing managed files in place.", exc,
                )
                return 0
            if inject_memory and definitions:
                # Defer the fetch into ``fetch_fn`` so an outage raises *inside*
                # read_memory (→ snapshot fallback) while a reachable-empty read
                # returns None (→ clears the snapshot, no injection).
                memory_fetch_fn = lambda: fetch_persona_memory(base_url, token)  # noqa: E731
    elif not inject_memory:
        memory = None

    # AG-10 memory doubling: route the read through the snapshot overlay so a
    # Superpos outage degrades to the read-only snapshot instead of dropping
    # MEMORY injection, a reachable read re-syncs the workspace snapshot, and a
    # reachable-empty (cleared) document clears it so we stop injecting stale
    # memory.  ttl_seconds=0 forces a fresh fetch every sync (the overlay only
    # owns the success-resync / clear-on-empty / outage-fallback edges).
    if inject_memory and memory_snapshot_dir is not None:
        mem_result = read_memory(
            memory_fetch_fn,
            snapshot_dir=memory_snapshot_dir,
            ttl_seconds=0,
        )
        memory = mem_result.content
        if mem_result.fetch_failed:
            log.warning(
                "MEMORY unavailable from Superpos; using %s snapshot",
                mem_result.source,
            )

    # Normalize an omitted memory (no snapshot overlay engaged) to None so it is
    # never injected or measured as a real value below.
    if memory is _MEMORY_OMITTED:
        memory = None

    if memory:
        log.info("Agent MEMORY: %d chars", len(memory))

    slugs = [d["slug"] for d in definitions if "slug" in d]

    local_context: str | None = None
    if modules_dir or skills_dir:
        local_context = discover_local_context(modules_dir, skills_dir, slugs)

    synced = 0
    for defn in definitions:
        slug = defn.get("slug")
        if not slug:
            continue

        content = build_subagent_md(
            defn,
            memory=memory,
            local_context=local_context,
        )

        target = Path(subagents_dir) / f"{slug}.md"
        target.write_text(content, encoding="utf-8")
        log.info("Synced sub-agent: %s (v%s)", slug, defn.get("version", "?"))
        synced += 1

    for existing in Path(subagents_dir).glob("*.md"):
        if existing.stem not in slugs:
            try:
                file_content = existing.read_text(encoding="utf-8")
            except OSError:
                continue
            if MANAGED_MARKER in file_content:
                existing.unlink()
                log.info("Removed stale managed sub-agent: %s", existing.stem)

    return synced


# ── CLI entry point ───────────────────────────────────────────────────


def _base_config() -> tuple[str, str]:
    """Read base URL and token from env."""
    base_url = os.environ.get("SUPERPOS_BASE_URL", "").rstrip("/")
    token = os.environ.get("SUPERPOS_API_TOKEN", "")
    if not base_url or not token:
        print(
            "Error: SUPERPOS_BASE_URL and SUPERPOS_API_TOKEN must be set",
            file=sys.stderr,
        )
        sys.exit(1)
    return base_url, token


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Sync Superpos SubAgentDefinitions to local subagent files",
    )
    parser.add_argument(
        "--subagents-dir",
        required=True,
        help="Target directory for subagent .md files (e.g. /workspace/.claude/subagents)",
    )
    parser.add_argument(
        "--inject-memory",
        action="store_true",
        help="Inject the agent's persona MEMORY into each subagent prompt",
    )
    parser.add_argument(
        "--inject-modules",
        action="store_true",
        help="Inject a summary of available modules, skills, and sibling subagents",
    )
    parser.add_argument(
        "--modules-dir",
        help="Modules directory to scan",
    )
    parser.add_argument(
        "--skills-dir",
        help="Skills directory to scan",
    )
    parser.add_argument(
        "--memory-snapshot-dir",
        help="Snapshot dir for MEMORY read fallback (AG-10 doubling)",
    )

    args = parser.parse_args()
    base_url, token = _base_config()

    count = sync_sub_agents(
        subagents_dir=args.subagents_dir,
        base_url=base_url,
        token=token,
        inject_memory=args.inject_memory,
        modules_dir=args.modules_dir if args.inject_modules else None,
        skills_dir=args.skills_dir if args.inject_modules else None,
        memory_snapshot_dir=args.memory_snapshot_dir,
    )

    print(f"Synced {count} sub-agent definition(s)")


if __name__ == "__main__":
    main()
