"""Thin async HTTP client for the Superpos REST API."""

from __future__ import annotations

import logging
import os
import warnings
from typing import Any, Mapping

import httpx

from .config import BaseConfig
from .redactor import redact


def _redact_summary(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not summary:
        return summary
    out: dict[str, Any] = {}
    for k, v in summary.items():
        out[k] = redact(v) if isinstance(v, str) else v
    return out


log = logging.getLogger(__name__)


class GitHubDiscoveryForbidden(Exception):
    """Raised when GitHub connection discovery is denied by the Superpos API.

    ``list_github_connections`` raises this on HTTP 401/403 (typically when the
    agent lacks the ``services.read`` permission) so callers can distinguish
    "no connection exists" from "we are not allowed to ask".  Callers that
    have a sensible fallback (e.g. the static ``GITHUB_TOKEN``) should catch
    it; callers that surface the result to the user (e.g. ``superpos-github
    connections``) should let it propagate so the user sees a clear
    permission error instead of an empty list.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class SuperposClient:
    def __init__(self, config: BaseConfig) -> None:
        self._config = config
        self._base_url = config.superpos_base_url.rstrip("/")
        self._token: str = config.superpos_api_token
        self._refresh_token: str = config.superpos_refresh_token
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=30.0,
            follow_redirects=True,
        )

    # ── Auth ──────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        masked = self._token[:8] + "..." if len(self._token) > 8 else "???"
        log.debug("Using token: %s (len=%d)", masked, len(self._token))
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

    async def refresh_auth(self) -> bool:
        """Try to refresh the API token. Returns True on success."""
        for endpoint, payload in [
            ("/api/v1/agents/token/refresh", {"refresh_token": self._refresh_token}),
            ("/api/v1/agents/refresh", {"refresh_token": self._refresh_token}),
            ("/api/v1/agents/login", {
                "agent_id": self._config.superpos_agent_id,
                "refresh_token": self._refresh_token,
            }),
        ]:
            try:
                resp = await self._client.post(
                    endpoint,
                    json=payload,
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json()
                self._token = data.get("token", self._token)
                if "refresh_token" in data:
                    self._refresh_token = data["refresh_token"]
                log.info("Superpos token refreshed via %s", endpoint)
                return True
            except httpx.HTTPStatusError:
                continue
        log.error("All refresh attempts failed")
        return False

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make a request, auto-refreshing token on 401.

        Callers may pass ``headers=`` to merge extra headers on top of the
        bearer token / Accept headers we always send.  Without this merge
        any caller-supplied ``headers`` would collide with the
        ``headers=self._headers()`` arg below and Python would raise
        ``TypeError: got multiple values for keyword argument 'headers'``
        before the request even left the client.
        """
        extra_headers = kwargs.pop("headers", None)

        def _final_headers() -> dict[str, str]:
            merged = self._headers()
            if extra_headers:
                merged.update(extra_headers)
            return merged

        resp = await self._client.request(
            method, path, headers=_final_headers(), **kwargs,
        )
        if resp.status_code == 401:
            log.warning("Superpos 401 — attempting token refresh")
            if await self.refresh_auth():
                resp = await self._client.request(
                    method, path, headers=_final_headers(), **kwargs,
                )
        resp.raise_for_status()
        return resp

    # ── Tasks ─────────────────────────────────────────────────────────

    async def poll_tasks(self) -> list[dict[str, Any]]:
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{self._config.superpos_hive_id}/tasks/poll",
            params={
                "capabilities": ",".join(self._config.superpos_capabilities),
            }
            if self._config.superpos_capabilities
            else None,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def claim_task(self, task_id: str) -> dict[str, Any]:
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "PATCH",
            f"/api/v1/hives/{hive}/tasks/{task_id}/claim",
        )
        return resp.json()

    async def complete_task(
        self, task_id: str, result: str, summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"result": {"output": redact(result)}}
        redacted_summary = _redact_summary(summary)
        if redacted_summary:
            body["summary"] = redacted_summary
        resp = await self._request(
            "PATCH",
            f"/api/v1/hives/{hive}/tasks/{task_id}/complete",
            json=body,
        )
        return resp.json()

    async def fail_task(
        self, task_id: str, error: str, summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"error": {"message": redact(error)}}
        redacted_summary = _redact_summary(summary)
        if redacted_summary:
            body["summary"] = redacted_summary
        resp = await self._request(
            "PATCH",
            f"/api/v1/hives/{hive}/tasks/{task_id}/fail",
            json=body,
        )
        return resp.json()

    async def create_task(
        self,
        task_type: str,
        payload: dict[str, Any] | None = None,
        target_agent_id: str | None = None,
        target_capability: str | None = None,
        priority: int = 2,
    ) -> dict[str, Any]:
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"type": task_type}
        if payload:
            body["payload"] = payload
        if target_agent_id:
            body["target_agent_id"] = target_agent_id
        if target_capability:
            body["target_capability"] = target_capability
        if priority != 2:
            body["priority"] = priority
        resp = await self._request("POST", f"/api/v1/hives/{hive}/tasks", json=body)
        return resp.json()

    async def create_schedule(
        self, name: str, trigger_type: str,
        task_type: str, task_payload: dict[str, Any] | None = None,
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        run_at: str | None = None,
        task_target_agent_id: str | None = None,
        overlap_policy: str = "skip",
    ) -> dict[str, Any]:
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {
            "name": name,
            "trigger_type": trigger_type,
            "task_type": task_type,
            "overlap_policy": overlap_policy,
        }
        if task_payload:
            body["task_payload"] = task_payload
        if cron_expression:
            body["cron_expression"] = cron_expression
        if interval_seconds:
            body["interval_seconds"] = interval_seconds
        if run_at:
            body["run_at"] = run_at
        if task_target_agent_id:
            body["task_target_agent_id"] = task_target_agent_id
        resp = await self._request("POST", f"/api/v1/hives/{hive}/schedules", json=body)
        return resp.json()

    async def list_schedules(self) -> list[dict[str, Any]]:
        hive = self._config.superpos_hive_id
        resp = await self._request("GET", f"/api/v1/hives/{hive}/schedules")
        data = resp.json()
        return data.get("data", []) if isinstance(data, dict) else data

    async def delete_schedule(self, schedule_id: str) -> None:
        hive = self._config.superpos_hive_id
        await self._request("DELETE", f"/api/v1/hives/{hive}/schedules/{schedule_id}")

    async def update_progress(self, task_id: str, progress: int) -> dict[str, Any]:
        """Report task progress (0-100). Resets progress_timeout on the server."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "PATCH",
            f"/api/v1/hives/{hive}/tasks/{task_id}/progress",
            json={"progress": progress},
        )
        return resp.json()

    async def heartbeat(
        self, *, model: str | None = None, effort: str | None = None,
    ) -> None:
        """Ping Superpos to stay online.

        Optionally reports the agent's current model/effort so the dashboard
        reflects live model state.  Fields are omitted when ``None`` — an
        older backend ignores unknown keys, and an agent with no tunable
        model sends the original empty body.
        """
        body: dict[str, Any] = {}
        if model:
            body["model"] = model
        if effort:
            body["effort"] = effort
        await self._request(
            "POST", "/api/v1/agents/heartbeat", json=body or None,
        )

    async def update_status(self, status: str) -> None:
        """Update agent status (online/busy/idle/offline/error)."""
        await self._request("PATCH", "/api/v1/agents/status", json={"status": status})

    async def fetch_me(self) -> dict[str, Any] | None:
        """Fetch the agent's server-side profile: hive_id, capabilities, permissions, etc."""
        try:
            resp = await self._request("GET", "/api/v1/agents/me")
            body = resp.json()
            return body.get("data", body) if isinstance(body, dict) else None
        except Exception:
            log.warning("Failed to fetch /agents/me", exc_info=True)
            return None

    # ── Persona ───────────────────────────────────────────────────────

    async def get_persona_assembled(self) -> str | None:
        """Fetch the pre-assembled persona system prompt. Returns None if unavailable."""
        try:
            resp = await self._request("GET", "/api/v1/persona/assembled")
            data = resp.json()
            persona_data = data.get("data", data) if isinstance(data, dict) else {}
            return persona_data.get("prompt") or None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.debug("Persona endpoint not available (404); proceeding without it")
            else:
                log.warning("Failed to fetch persona; proceeding without it", exc_info=True)
            return None
        except Exception:
            log.warning("Failed to fetch persona; proceeding without it", exc_info=True)
            return None

    async def get_persona_version(
        self,
        known_version: int | None = None,
        known_platform_version: int | None = None,
        known_environment_version: str | None = None,
    ) -> dict[str, Any]:
        """Check the server-assigned persona / platform / environment versions.

        Lightweight poll-friendly call.  ``known_*`` params let the server
        compute the ``changed`` flag in one round trip without the client
        having to compare manually.  ``environment_version`` is a content
        hash (hex string), not an integer like the other two.
        """
        try:
            params: dict[str, Any] = {}
            if known_version is not None:
                params["known_version"] = known_version
            if known_platform_version is not None:
                params["known_platform_version"] = known_platform_version
            if known_environment_version is not None:
                params["known_environment_version"] = known_environment_version
            resp = await self._request("GET", "/api/v1/persona/version", params=params or None)
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.debug("Persona version endpoint not available (404)")
            else:
                log.warning("Failed to check persona version", exc_info=True)
            return {}
        except Exception:
            log.warning("Failed to check persona version", exc_info=True)
            return {}

    async def update_persona_memory(
        self,
        content: str,
        message: str | None = None,
        mode: str = "append",
    ) -> dict[str, Any]:
        """Update the MEMORY document in the active persona."""
        body: dict[str, Any] = {"content": content, "mode": mode}
        if message:
            body["message"] = message
        resp = await self._request("PATCH", "/api/v1/persona/memory", json=body)
        return resp.json()

    # ── Knowledge (read-only) ─────────────────────────────────────────

    async def search_knowledge(
        self,
        q: str | None = None,
        *,
        scope: str | None = None,
        mode: str | None = None,
        explain: bool = False,
        semantic: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """``GET /knowledge/search`` — hybrid / FTS / semantic knowledge search.

        The server requires at least one of ``q`` or ``scope`` and returns
        400 otherwise; we raise ``ValueError`` here instead so a caller
        mistake fails fast and synchronously rather than as a delayed
        ``httpx.HTTPStatusError`` from the network.

        ``mode`` selects the ranking strategy: ``"hybrid"`` (default, RRF
        fusion of FTS + pgvector + read-count + recency), ``"fts"``
        (Postgres ``ts_query`` / ``ts_rank``), or ``"semantic"`` (pgvector
        cosine).  When ``mode`` is ``None`` the server picks its own
        default (currently ``hybrid``).  Pass ``explain=True`` to receive
        a per-result ``score_breakdown`` for debugging weights.

        ``semantic=True`` is the deprecated alias for ``mode="semantic"``
        and emits :class:`DeprecationWarning`.  ``mode`` wins if both are
        set.

        Returns the unwrapped entry list — pagination meta (``total``,
        ``query``, ``mode``) is on the envelope; callers needing it
        should hit the raw endpoint via ``_request`` directly.
        """
        if q is None and scope is None:
            raise ValueError(
                "search_knowledge requires at least one of `q` or `scope`",
            )
        if semantic:
            warnings.warn(
                "search_knowledge(semantic=True) is deprecated; "
                "use mode='semantic' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            if mode is None:
                mode = "semantic"
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {"limit": limit}
        if q is not None:
            params["q"] = q
        if scope is not None:
            params["scope"] = scope
        if mode is not None:
            params["mode"] = mode
        if explain:
            params["explain"] = "true"
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{hive}/knowledge/search",
            params=params,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def list_knowledge(
        self,
        *,
        key: str | None = None,
        scope: str | None = None,
        tags: str | None = None,
        stale_days: int | None = None,
        sort: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """``GET /knowledge`` — filtered listing.

        - ``key`` accepts SQL wildcards (``*`` mapped to ``%`` server-side).
        - ``tags`` is comma-separated; results must contain ALL listed tags.
        - ``stale_days`` filters to entries not read in N days (great for
          finding what needs refreshing).
        - ``sort="least_read"`` orders ascending by read count.
        - ``limit`` is clamped 1-100 server-side; default 50.
        """
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {"limit": limit}
        if key is not None:
            params["key"] = key
        if scope is not None:
            params["scope"] = scope
        if tags is not None:
            params["tags"] = tags
        if stale_days is not None:
            params["stale_days"] = stale_days
        if sort is not None:
            params["sort"] = sort
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{hive}/knowledge",
            params=params,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_knowledge(self, entry_id: str) -> dict[str, Any]:
        """``GET /knowledge/{entry}`` — fetch a single entry."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/knowledge/{entry_id}",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_knowledge_by_slug(self, slug: str) -> dict[str, Any]:
        """Fetch a single entry by its stable human-readable slug.

        There is no ``GET /knowledge/slug/{slug}`` route on the server, so
        this is a two-hop over existing endpoints:

        1. :meth:`search_knowledge` (``GET /knowledge/search``) resolves the
           slug to an entry — we search with the slug as the query and require
           a result whose ``slug`` field equals ``slug`` exactly. Because the
           search is a relevance search over entry text, a non-exact candidate
           may be unrelated, so we do not fall back to it.
        2. :meth:`get_knowledge` (``GET /knowledge/{entry}``) fetches the full
           entry by its ULID.

        Raises :class:`ValueError` if the search returns no exact slug match or
        the resolved candidate carries no ``id`` — a clear "not found" rather
        than a delayed HTTP error or an unrelated relevance hit.
        """
        results = await self.search_knowledge(slug, limit=10)
        match = next(
            (
                r for r in results
                if isinstance(r, dict) and r.get("slug") == slug
            ),
            None,
        )
        if match is None:
            raise ValueError(f"no knowledge entry found for slug {slug!r}")
        entry_id = match.get("id") if isinstance(match, dict) else None
        if not entry_id:
            raise ValueError(
                f"knowledge entry for slug {slug!r} has no id to fetch by",
            )
        return await self.get_knowledge(str(entry_id))

    async def list_knowledge_by_type(
        self,
        type: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /knowledge/types/{type}/list`` — list entries of a given type.

        Hits the dedicated ``/types/{type}/list`` endpoint (handled by
        ``KnowledgeController::listByType``), which validates ``type``
        server-side against ``FrontmatterSchema::TYPES``.  The valid values
        are ``entity``, ``topic``, ``trend``, ``source_page``, ``log``,
        ``procedure`` — the script validates against that set before the
        network round-trip.
        """
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{hive}/knowledge/types/{type}/list",
            params=params,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def list_knowledge_backlinks(self, entry_id: str) -> list[dict[str, Any]]:
        """``GET /knowledge/{entry}/backlinks`` — entries that link to this entry.

        ``entry_id`` is a ULID (not a slug) — the server resolves the entry
        by primary key.  To find the ULID for a given slug, do::

            search <slug> --limit 1 | jq '.[0].id'

        Inverse of wikilink resolution: surfaces typed pages whose body
        (or frontmatter) contains ``[[<slug-of-entry>]]``.
        """
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/knowledge/{entry_id}/backlinks",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_knowledge_graph(
        self,
        entry_id: str,
        *,
        depth: int = 2,
        max_nodes: int = 50,
        link_types: str | None = None,
    ) -> dict[str, Any]:
        """``GET /knowledge/{entry}/graph`` — BFS link traversal.

        Server clamps ``depth`` to 1-5 and ``max_nodes`` to 1-200.
        ``link_types`` is a comma-separated allowlist (e.g.
        ``"decides,depends_on"``) — omit to include every link type.
        """
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {"depth": depth, "max_nodes": max_nodes}
        if link_types:
            params["link_types"] = link_types
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{hive}/knowledge/{entry_id}/graph",
            params=params,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def knowledge_topics(self) -> dict[str, Any]:
        """``GET /knowledge/index/topics`` — convenience index of topic clusters."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/knowledge/index/topics",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def knowledge_decisions(self) -> dict[str, Any]:
        """``GET /knowledge/index/decisions`` — convenience index of decisions."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/knowledge/index/decisions",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    # ── Knowledge (write) ─────────────────────────────────────────────

    async def create_knowledge(
        self,
        *,
        key: str,
        value: Any,
        scope: str | None = None,
        visibility: str | None = None,
        ttl: str | None = None,
    ) -> dict[str, Any]:
        """``POST /knowledge`` — create a new entry."""
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"key": key, "value": value}
        if scope is not None:
            body["scope"] = scope
        if visibility is not None:
            body["visibility"] = visibility
        if ttl is not None:
            body["ttl"] = ttl
        resp = await self._request("POST", f"/api/v1/hives/{hive}/knowledge", json=body)
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def update_knowledge(
        self,
        entry_id: str,
        *,
        value: Any,
        visibility: str | None = None,
        ttl: str | None = None,
    ) -> dict[str, Any]:
        """``PUT /knowledge/{entry}`` — replace value (bumps version)."""
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"value": value}
        if visibility is not None:
            body["visibility"] = visibility
        if ttl is not None:
            body["ttl"] = ttl
        resp = await self._request(
            "PUT", f"/api/v1/hives/{hive}/knowledge/{entry_id}", json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def create_knowledge_page(
        self,
        *,
        type: str,
        slug: str,
        body: str,
        title: str | None = None,
        summary: str | None = None,
        frontmatter: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        source_ids: list[str] | None = None,
        scope: str | None = None,
        visibility: str | None = None,
        ttl: str | None = None,
    ) -> dict[str, Any]:
        """``POST /knowledge`` with the typed `type`+`slug`+`body` shape (TASK-297).

        Mirrors ``create_knowledge`` (the legacy ``key``+``value`` shape) but sends
        the typed-page payload the platform has shipped server-side.  Same
        envelope/response contract.
        """
        hive = self._config.superpos_hive_id
        payload: dict[str, Any] = {"type": type, "slug": slug, "body": body}
        if title is not None:
            payload["title"] = title
        if summary is not None:
            payload["summary"] = summary
        if frontmatter is not None:
            payload["frontmatter"] = frontmatter
        if tags is not None:
            payload["tags"] = tags
        if source_ids is not None:
            payload["source_ids"] = source_ids
        if scope is not None:
            payload["scope"] = scope
        if visibility is not None:
            payload["visibility"] = visibility
        if ttl is not None:
            payload["ttl"] = ttl
        resp = await self._request("POST", f"/api/v1/hives/{hive}/knowledge", json=payload)
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def update_knowledge_page(
        self,
        entry_id: str,
        *,
        body: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        frontmatter: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        source_ids: list[str] | None = None,
        visibility: str | None = None,
        ttl: str | None = None,
    ) -> dict[str, Any]:
        """``PUT /knowledge/{entry}`` with the typed shape (TASK-297).

        Partial update: only the supplied fields are sent.  ``type`` and ``slug``
        are intentionally not accepted here — re-typing a page or changing its
        slug would invalidate inbound wikilinks; the dedicated migration path
        handles those, not the CLI.
        """
        hive = self._config.superpos_hive_id
        payload: dict[str, Any] = {}
        if body is not None:
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if summary is not None:
            payload["summary"] = summary
        if frontmatter is not None:
            payload["frontmatter"] = frontmatter
        if tags is not None:
            payload["tags"] = tags
        if source_ids is not None:
            payload["source_ids"] = source_ids
        if visibility is not None:
            payload["visibility"] = visibility
        if ttl is not None:
            payload["ttl"] = ttl
        resp = await self._request(
            "PUT", f"/api/v1/hives/{hive}/knowledge/{entry_id}", json=payload,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def delete_knowledge(self, entry_id: str) -> None:
        """``DELETE /knowledge/{entry}``."""
        hive = self._config.superpos_hive_id
        await self._request("DELETE", f"/api/v1/hives/{hive}/knowledge/{entry_id}")

    async def list_knowledge_links(
        self,
        *,
        source_id: str | None = None,
        target_id: str | None = None,
        target_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /knowledge/links`` — list links filtered by source/target."""
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {}
        if source_id is not None:
            params["source"] = source_id
        if target_id is not None:
            params["target"] = target_id
        if target_type is not None:
            params["target_type"] = target_type
        if limit is not None:
            params["limit"] = limit
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/knowledge/links",
            params=params or None,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def create_knowledge_link(
        self,
        entry_id: str,
        *,
        target_id: str | None = None,
        target_ref: str | None = None,
        target_type: str = "knowledge",
        link_type: str = "relates_to",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /knowledge/{entry}/links`` — link an entry to another entity."""
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"target_type": target_type, "link_type": link_type}
        if target_id is not None:
            body["target_id"] = target_id
        if target_ref is not None:
            body["target_ref"] = target_ref
        if metadata is not None:
            body["metadata"] = metadata
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/knowledge/{entry_id}/links",
            json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def delete_knowledge_link(self, link_id: str) -> None:
        """``DELETE /knowledge/links/{link}``."""
        hive = self._config.superpos_hive_id
        await self._request(
            "DELETE", f"/api/v1/hives/{hive}/knowledge/links/{link_id}",
        )

    async def confirm_knowledge_link(self, link_id: str) -> dict[str, Any]:
        """``POST /knowledge/links/{link}/confirm`` — promote suggested → confirmed."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/knowledge/links/{link_id}/confirm",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def dismiss_knowledge_link(self, link_id: str) -> dict[str, Any]:
        """``DELETE /knowledge/links/{link}/dismiss`` — exclude from future suggestions."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "DELETE", f"/api/v1/hives/{hive}/knowledge/links/{link_id}/dismiss",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    # ── Issues ────────────────────────────────────────────────────────

    async def list_issues(
        self,
        *,
        state: str | None = None,
        issue_type_id: str | None = None,
        assignee_id: str | None = None,
        q: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> dict[str, Any]:
        """``GET /issues`` — paginated list with optional filters.

        Returns the full envelope (``{"data": [...], "meta": {...}}``) because
        callers need ``meta.has_more`` / ``meta.current_page`` to paginate.
        Pass ``page=2`` (etc.) to advance past the first page — Laravel's
        ``simplePaginate`` honours the standard ``?page=`` query param.
        """
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {}
        if state is not None:
            params["state"] = state
        if issue_type_id is not None:
            params["issue_type_id"] = issue_type_id
        if assignee_id is not None:
            params["assignee_id"] = assignee_id
        if q is not None:
            params["q"] = q
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/issues", params=params or None,
        )
        return resp.json()

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        """``GET /issues/{issue}`` — full issue with relations (type, tasks,
        dependencies, channel, thread, pending approvals)."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/issues/{issue_id}",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def create_issue(
        self,
        *,
        title: str,
        issue_type_id: str,
        description: str | None = None,
        assignee_type: str | None = None,
        assignee_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /issues`` — open a new issue in this hive."""
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"title": title, "issue_type_id": issue_type_id}
        if description is not None:
            body["description"] = description
        if assignee_type is not None:
            body["assignee_type"] = assignee_type
        if assignee_id is not None:
            body["assignee_id"] = assignee_id
        if metadata is not None:
            body["metadata"] = metadata
        if channel_id is not None:
            body["channel_id"] = channel_id
        if thread_id is not None:
            body["thread_id"] = thread_id
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/issues", json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def update_issue(
        self,
        issue_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        assignee_type: str | None = None,
        assignee_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        issue_type_id: str | None = None,
        channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """``PATCH /issues/{issue}`` — partial update; omitted fields stay put."""
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {}
        for field, value in (
            ("title", title),
            ("description", description),
            ("assignee_type", assignee_type),
            ("assignee_id", assignee_id),
            ("metadata", metadata),
            ("issue_type_id", issue_type_id),
            ("channel_id", channel_id),
            ("thread_id", thread_id),
        ):
            if value is not None:
                body[field] = value
        if not body:
            raise ValueError("update_issue requires at least one field to change")
        resp = await self._request(
            "PATCH", f"/api/v1/hives/{hive}/issues/{issue_id}", json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def transition_issue(
        self,
        issue_id: str,
        *,
        to: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """``POST /issues/{issue}/transition`` — drive the issue state machine.

        ``to`` is one of the platform's ``Issue::STATES`` values
        (e.g. ``in_progress``, ``awaiting_review``, ``done``, ``blocked``,
        ``cancelled``).  Server returns 422 if the transition is illegal
        from the current state.
        """
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"to": to}
        if reason is not None:
            body["reason"] = reason
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/issues/{issue_id}/transition",
            json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def close_issue(
        self, issue_id: str, *, reason: str | None = None,
    ) -> dict[str, Any]:
        """``POST /issues/{issue}/close`` — policy-aware close.

        The server consults the issue type's ``closure_policy``: a direct
        close to ``done`` happens when allowed; otherwise the call either
        moves the issue to ``awaiting_review`` or creates a closure
        ``ApprovalRequest`` (issue goes to ``blocked``).  Callers should
        inspect ``state`` on the returned issue to know which path ran.
        """
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {}
        if reason is not None:
            body["reason"] = reason
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/issues/{issue_id}/close",
            json=body or None,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def link_task_to_issue(
        self, issue_id: str, *, task_id: str,
    ) -> dict[str, Any]:
        """``POST /issues/{issue}/link-task`` — attach an existing task to this issue."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/issues/{issue_id}/link-task",
            json={"task_id": task_id},
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def link_channel_to_issue(
        self, issue_id: str, *, channel_id: str,
    ) -> dict[str, Any]:
        """``POST /issues/{issue}/link-channel`` — bind a channel to this issue."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/issues/{issue_id}/link-channel",
            json={"channel_id": channel_id},
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def link_issue_to_track(
        self, track_slug: str, issue_id: str,
    ) -> dict[str, Any]:
        """``POST /tracks/{slug}/issues`` — link an existing issue to a track.

        The track is addressed by ``slug`` in the URL path; the body carries
        the issue id, mirroring the platform's ``TrackController::linkIssue``
        (returns ``{"track_id", "issue_id"}``). Requires ``issues.manage``.
        """
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/tracks/{track_slug}/issues",
            json={"issue_id": issue_id},
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def request_issue_approval(
        self,
        issue_id: str,
        *,
        summary: str,
        recommended_action: str | None = None,
        risks: str | None = None,
        linked_issue_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """``POST /issues/{issue}/request-approval`` — escalate for human review.

        Only valid when the issue is ``in_progress`` or ``blocked``.  The
        server creates a pending ``ApprovalRequest`` and (from
        ``in_progress``) drives the issue to ``blocked``.  Concurrent
        duplicate calls return 422 ``invalid_state``.
        """
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"summary": summary}
        if recommended_action is not None:
            body["recommended_action"] = recommended_action
        if risks is not None:
            body["risks"] = risks
        if linked_issue_ids is not None:
            body["linked_issue_ids"] = linked_issue_ids
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/issues/{issue_id}/request-approval",
            json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def create_issue_dependency(
        self,
        issue_id: str,
        *,
        depends_on_issue_id: str,
        kind: str,
    ) -> dict[str, Any]:
        """``POST /issues/{issue}/dependencies`` — declare a blocking
        relationship to another issue (``kind`` per platform enum, e.g.
        ``blocks``, ``related_to``)."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/issues/{issue_id}/dependencies",
            json={
                "depends_on_issue_id": depends_on_issue_id,
                "kind": kind,
            },
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def delete_issue_dependency(
        self, issue_id: str, dependency_id: str,
    ) -> None:
        """``DELETE /issues/{issue}/dependencies/{dependency}``."""
        hive = self._config.superpos_hive_id
        await self._request(
            "DELETE",
            f"/api/v1/hives/{hive}/issues/{issue_id}/dependencies/{dependency_id}",
        )

    # ── Tracks ────────────────────────────────────────────────────────

    async def list_tracks(
        self,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /tracks`` — list tracks in the hive. ``spec`` is omitted.

        ``status`` is forwarded to the server as a query param (forward-
        compatible if the index gains support later), but state filtering is
        ALSO enforced client-side because the server index does not filter:
        ``TrackController::index`` only scopes by hive and orders by
        ``updated_at``, ignoring query params. Rows are kept only when their
        ``state`` equals ``status``; rows missing a ``state`` field are
        excluded while a filter is active so unknown-state rows never leak.
        """
        hive = self._config.superpos_hive_id
        params: dict[str, str] = {}
        if status is not None:
            params["status"] = status
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/tracks", params=params or None,
        )
        data = resp.json()
        rows = data.get("data", data) if isinstance(data, dict) else data
        if status is None:
            return rows
        return [row for row in rows if row.get("state") == status]

    async def get_track_by_slug(self, slug: str) -> dict[str, Any]:
        """``GET /tracks/{slug}`` — fetch a single track including ``spec``."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/tracks/{slug}",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def create_track(
        self,
        *,
        slug: str,
        name: str,
        description: str | None = None,
        spec: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """``POST /tracks`` — create a track (returns 201 with full payload)."""
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"slug": slug, "name": name}
        if description is not None:
            body["description"] = description
        if spec is not None:
            body["spec"] = spec
        if state is not None:
            body["state"] = state
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/tracks", json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def patch_track(
        self,
        slug: str,
        *,
        name: str | None = None,
        description: str | None = None,
        spec: str | None = None,
    ) -> dict[str, Any]:
        """``PATCH /tracks/{slug}`` — update name/description/spec.

        ``state`` transitions go through ``POST /tracks/{slug}/transition``,
        not this method.  Slug is immutable.
        """
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if spec is not None:
            body["spec"] = spec
        resp = await self._request(
            "PATCH", f"/api/v1/hives/{hive}/tracks/{slug}", json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def link_track_issue(
        self, slug: str, issue_id: str,
    ) -> dict[str, Any]:
        """``POST /tracks/{slug}/issues`` — link an issue to a track.

        Mirrors ``link_issue_to_track`` under a track-centric name so the
        tracks CLI has a 1:1 method-to-subcommand mapping.
        """
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/tracks/{slug}/issues",
            json={"issue_id": issue_id},
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def unlink_track_issue(
        self, slug: str, issue_id: str,
    ) -> None:
        """``DELETE /tracks/{slug}/issues/{issue_id}`` — unlink (204 No Content)."""
        hive = self._config.superpos_hive_id
        await self._request(
            "DELETE", f"/api/v1/hives/{hive}/tracks/{slug}/issues/{issue_id}",
        )

    async def list_track_issues(
        self, slug: str, *, page: int | None = None, per_page: int | None = None,
    ) -> dict[str, Any]:
        """``GET /tracks/{slug}/issues`` — paginated list of issues linked to a track.

        Returns the full envelope (``{"data": [...], "meta": {...}}``) so callers
        can paginate via ``meta.has_more`` / ``meta.current_page``. This is the
        read-side counterpart to ``link_track_issue`` / ``unlink_track_issue``;
        closes the gap that ``get_track_by_slug`` only returns the track record
        (the dashboard's linked-issues panel reads from this endpoint).
        """
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/tracks/{slug}/issues",
            params=params or None,
        )
        return resp.json()

    # ── Issue types ───────────────────────────────────────────────────

    async def list_issue_types(self) -> list[dict[str, Any]]:
        """``GET /issue-types`` — issue-type catalogue for this hive."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/issue-types",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    # ── Attachments (file uploads) ────────────────────────────────────

    async def upload_attachment(
        self,
        *,
        file_path: str,
        issue_id: str | None = None,
        task_id: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """``POST /attachments`` — multipart upload of a file.

        Optionally associate the file with an issue (``issue_id``) and/or a
        task (``task_id``); both must live in this hive or the server returns
        422.  Only file attachments are supported — there is no URL/link form.
        """
        hive = self._config.superpos_hive_id
        data: dict[str, Any] = {}
        if issue_id is not None:
            data["issue_id"] = issue_id
        if task_id is not None:
            data["task_id"] = task_id
        if description is not None:
            data["description"] = description

        # httpx closes the handle when the request body is consumed.
        with open(file_path, "rb") as fh:
            resp = await self._request(
                "POST",
                f"/api/v1/hives/{hive}/attachments",
                data=data or None,
                files={"file": (os.path.basename(file_path), fh)},
            )
        body = resp.json()
        return body.get("data", body) if isinstance(body, dict) else body

    async def list_attachments(
        self,
        *,
        issue_id: str | None = None,
        task_id: str | None = None,
        search: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> dict[str, Any]:
        """``GET /attachments`` — list attachments, optionally filtered.

        Returns the full envelope (callers need ``meta`` for pagination).
        Use ``page`` to walk past the first page (see ``meta.current_page`` /
        ``meta.last_page``).
        """
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {}
        if issue_id is not None:
            params["issue_id"] = issue_id
        if task_id is not None:
            params["task_id"] = task_id
        if search is not None:
            params["search"] = search
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/attachments", params=params or None,
        )
        return resp.json()

    async def delete_attachment(self, attachment_id: str) -> None:
        """``DELETE /attachments/{attachment}`` — remove a file + its record."""
        hive = self._config.superpos_hive_id
        await self._request(
            "DELETE", f"/api/v1/hives/{hive}/attachments/{attachment_id}",
        )

    # ── Workflows ─────────────────────────────────────────────────────

    async def list_workflows(
        self,
        *,
        is_active: bool | None = None,
        search: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> dict[str, Any]:
        """``GET /workflows`` — paginated list with optional filters.

        Server-side query params are ``is_active`` (boolean filter) and
        ``search`` (case-insensitive substring against name/slug); the
        kwargs here mirror those names exactly so the filters are not
        silently dropped.

        Returns the full envelope so callers can paginate via
        ``meta.has_more`` / ``meta.current_page``.
        """
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {}
        if is_active is not None:
            params["is_active"] = "true" if is_active else "false"
        if search is not None:
            params["search"] = search
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/workflows", params=params or None,
        )
        return resp.json()

    async def get_workflow(self, workflow_id_or_slug: str) -> dict[str, Any]:
        """``GET /workflows/{id}`` — fetch by ID or slug (server resolves both)."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/workflows/{workflow_id_or_slug}",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def create_workflow(
        self,
        *,
        name: str,
        slug: str,
        trigger_config: Mapping[str, Any],
        steps: Mapping[str, Any],
        description: str | None = None,
        settings: Mapping[str, Any] | None = None,
        is_active: bool = True,
    ) -> dict[str, Any]:
        """``POST /workflows`` — create a new workflow definition (v1).

        ``steps`` is a mapping keyed by step name (e.g.
        ``{"plan": {...}, "build": {...}}``); the executor uses those
        keys as the canonical step IDs that ``next`` / ``then`` /
        ``depends_on_steps`` reference. Shape validation is the
        server's job — anything JSON-serialisable is forwarded as-is.
        """
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {
            "name": name,
            "slug": slug,
            "trigger_config": trigger_config,
            "steps": steps,
            "is_active": is_active,
        }
        if description is not None:
            body["description"] = description
        if settings is not None:
            body["settings"] = settings
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/workflows", json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def update_workflow(
        self,
        workflow_id: str,
        *,
        name: str | None = None,
        slug: str | None = None,
        trigger_config: Mapping[str, Any] | None = None,
        steps: Mapping[str, Any] | None = None,
        description: str | None = None,
        settings: Mapping[str, Any] | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any]:
        """``PUT /workflows/{id}`` — full-shape update; snapshots a new
        ``WorkflowVersion`` when ``steps`` / ``trigger_config`` / ``settings``
        change.

        ``steps`` is a mapping keyed by step name; see
        :meth:`create_workflow` for shape details.
        """
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {}
        for field, value in (
            ("name", name),
            ("slug", slug),
            ("trigger_config", trigger_config),
            ("steps", steps),
            ("description", description),
            ("settings", settings),
            ("is_active", is_active),
        ):
            if value is not None:
                body[field] = value
        if not body:
            raise ValueError("update_workflow requires at least one field to change")
        resp = await self._request(
            "PUT", f"/api/v1/hives/{hive}/workflows/{workflow_id}", json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def delete_workflow(self, workflow_id: str) -> None:
        """``DELETE /workflows/{id}`` — server refuses if active runs exist."""
        hive = self._config.superpos_hive_id
        await self._request(
            "DELETE", f"/api/v1/hives/{hive}/workflows/{workflow_id}",
        )

    async def list_workflow_versions(
        self, workflow_id: str,
    ) -> list[dict[str, Any]]:
        """``GET /workflows/{id}/versions`` — version history (newest first)."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/workflows/{workflow_id}/versions",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_workflow_version(
        self, workflow_id: str, version: int,
    ) -> dict[str, Any]:
        """``GET /workflows/{id}/versions/{n}`` — a single immutable snapshot."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{hive}/workflows/{workflow_id}/versions/{version}",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def diff_workflow_versions(
        self, workflow_id: str, from_version: int, to_version: int,
    ) -> dict[str, Any]:
        """``GET /workflows/{id}/versions/{from}/diff/{to}`` — structural diff."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{hive}/workflows/{workflow_id}"
            f"/versions/{from_version}/diff/{to_version}",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def rollback_workflow_version(
        self, workflow_id: str, version: int,
    ) -> dict[str, Any]:
        """``POST /workflows/{id}/versions/{n}/rollback`` — restore an
        older snapshot as the new head version."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "POST",
            f"/api/v1/hives/{hive}/workflows/{workflow_id}"
            f"/versions/{version}/rollback",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def list_workflow_runs(
        self,
        workflow_id: str,
        *,
        status: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> dict[str, Any]:
        """``GET /workflows/{id}/runs`` — paginated runs index."""
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{hive}/workflows/{workflow_id}/runs",
            params=params or None,
        )
        return resp.json()

    async def get_workflow_run(
        self, workflow_id: str, run_id: str,
    ) -> dict[str, Any]:
        """``GET /workflows/{id}/runs/{run}`` — single run with ``thread`` and
        ``step_states`` embedded."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{hive}/workflows/{workflow_id}/runs/{run_id}",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def start_workflow_run(
        self,
        workflow_id: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /workflows/{id}/runs`` — kick off a new run.

        Body is ``{"payload": ...}`` when ``payload`` is provided, else an
        empty object — the server requires a JSON body either way.
        """
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"payload": payload} if payload is not None else {}
        resp = await self._request(
            "POST",
            f"/api/v1/hives/{hive}/workflows/{workflow_id}/runs",
            json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def cancel_workflow_run(
        self, workflow_id: str, run_id: str,
    ) -> dict[str, Any]:
        """``POST /workflows/{id}/runs/{run}/cancel`` — best-effort cancel."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "POST",
            f"/api/v1/hives/{hive}/workflows/{workflow_id}"
            f"/runs/{run_id}/cancel",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def retry_workflow_run(
        self, workflow_id: str, run_id: str,
    ) -> dict[str, Any]:
        """``POST /workflows/{id}/runs/{run}/retry`` — re-run a failed
        run from the failed step (or from start if non-resumable)."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "POST",
            f"/api/v1/hives/{hive}/workflows/{workflow_id}"
            f"/runs/{run_id}/retry",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    # ── Sub-agents (read) ─────────────────────────────────────────────

    async def list_sub_agents(self) -> list[dict[str, Any]]:
        """``GET /sub-agents`` — list active sub-agent definitions in this hive."""
        resp = await self._request("GET", "/api/v1/sub-agents")
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_sub_agent(self, slug: str) -> dict[str, Any]:
        """``GET /sub-agents/{slug}`` — current active version by slug."""
        resp = await self._request("GET", f"/api/v1/sub-agents/{slug}")
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_sub_agent_by_id(self, sub_agent_id: str) -> dict[str, Any]:
        """``GET /sub-agents/by-id/{id}`` — version-stable lookup by ULID."""
        resp = await self._request(
            "GET", f"/api/v1/sub-agents/by-id/{sub_agent_id}",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_sub_agent_assembled(self, slug: str) -> str | None:
        """``GET /sub-agents/{slug}/assembled`` — pre-assembled system prompt."""
        resp = await self._request(
            "GET", f"/api/v1/sub-agents/{slug}/assembled",
        )
        data = resp.json()
        payload = data.get("data", data) if isinstance(data, dict) else {}
        return payload.get("prompt") if isinstance(payload, dict) else None

    async def get_sub_agent_assembled_by_id(self, sub_agent_id: str) -> str | None:
        """``GET /sub-agents/by-id/{id}/assembled`` — pre-assembled by ULID."""
        resp = await self._request(
            "GET", f"/api/v1/sub-agents/by-id/{sub_agent_id}/assembled",
        )
        data = resp.json()
        payload = data.get("data", data) if isinstance(data, dict) else {}
        return payload.get("prompt") if isinstance(payload, dict) else None

    async def get_runtime_bundle(self) -> dict[str, Any] | None:
        """``GET /sub-agents/runtime-bundle`` — all definitions + agent memory in one call.

        Returns dict with ``definitions``, ``agent_memory``, ``persona_version``
        or ``None`` if the endpoint is not available (older backend).
        """
        try:
            resp = await self._request("GET", "/api/v1/sub-agents/runtime-bundle")
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        payload = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(payload, dict) or "definitions" not in payload:
            return None
        return payload

    async def get_registry_resolved(self) -> dict[str, Any] | None:
        """``GET /registry/resolved`` — registry-served skills + modules (+ subagents).

        Beat 2a (superpos-app) added grouped top-level keys to the resolved
        response alongside the existing flat ``items`` list:

        - ``skills``  — ``[{slug, name, revision_id, instructions, files, ...}]``
        - ``modules`` — ``[{slug, name, revision_id, manifest, files, install, skill, ...}]``
        - ``subagents`` — not consumed agent-side here.

        Returns the parsed payload dict (already unwrapped from the
        ``{"data": ...}`` envelope), or ``None`` if the endpoint is
        unavailable / returns a non-200 / bad shape.  Callers treat
        ``None`` as "fall back to baked-in" — same defensive posture as
        :meth:`get_runtime_bundle`.
        """
        try:
            resp = await self._request("GET", "/api/v1/registry/resolved")
        except Exception:
            log.warning("Registry resolved fetch failed", exc_info=True)
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("Registry resolved response was not JSON")
            return None
        payload = data.get("data", data) if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return None
        return payload

    # ── Service proxy ─────────────────────────────────────────────────

    async def discover_services(
        self,
        *,
        capability_prefix: str = "data:",
    ) -> list[dict[str, Any]]:
        """``GET /services`` — list service workers registered in this hive.

        Filters to agents whose capability list contains entries with the
        given prefix (default ``data:``).  Each record carries
        ``metadata.supported_operations`` for callers that need to decide
        which operation to invoke through ``service_request``.
        """
        params = {"capability_prefix": capability_prefix}
        resp = await self._request(
            "GET", "/api/v1/services", params=params,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def service_request(
        self,
        method: str,
        service: str,
        path: str = "",
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Forward an HTTP request through Superpos's credentialed proxy.

        ``GET/POST/PUT/PATCH/DELETE /proxy/{service}/{path}`` — Superpos
        injects the connection's stored OAuth/API credentials before
        forwarding, so the agent never sees them.

        Returns the raw ``httpx.Response`` because the target service can
        return any content type (JSON, XML, binary).  Callers decide
        whether to ``.json()``, ``.text``, or ``.content`` based on the
        target API.
        """
        endpoint = f"/api/v1/proxy/{service}"
        if path:
            endpoint = f"{endpoint}/{path.lstrip('/')}"
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        if headers is not None:
            # _request merges these on top of the bearer-token headers.
            kwargs["headers"] = headers
        return await self._request(method, endpoint, **kwargs)

    # ── GitHub (service catalog + token broker) ───────────────────────

    async def list_github_connections(
        self,
        *,
        status: str = "active",
    ) -> list[dict[str, Any]]:
        """``GET /hives/{hive}/services?type=github`` — GitHub service connections.

        Each record carries ``id`` (the ``service_connection_id`` the token
        broker expects), ``name`` (the key the credentialed proxy resolves on),
        and ``metadata.auth_type`` (``github_app`` connections can be brokered
        into short-lived installation tokens; ``token``/PAT connections cannot —
        they must go through the proxy or the static ``GITHUB_TOKEN`` fallback).

        The services catalog is paginated (Laravel's default ``per_page`` is
        15), so a single request would silently miss GitHub connections beyond
        the first page.  We request a large page size and walk every page,
        following the response ``meta`` (``has_more`` / ``current_page`` /
        ``last_page``) and falling back to the returned batch size so an absent
        or unfamiliar ``meta`` can never loop forever.

        Requires the ``services.read`` permission.  If the agent lacks it (HTTP
        401/403) this raises :class:`GitHubDiscoveryForbidden` so callers can
        distinguish "no connection exists" (returns ``[]``) from "we are not
        allowed to ask" (raise).  Callers with a static-credential fallback
        (e.g. ``GITHUB_TOKEN``) should catch the exception and fall through;
        callers that surface the result to the user should let it propagate
        so the user sees a clear permission error.
        """
        hive = self._config.superpos_hive_id
        per_page = 100
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            try:
                resp = await self._request(
                    "GET",
                    f"/api/v1/hives/{hive}/services",
                    params={
                        "type": "github",
                        "status": status,
                        "page": page,
                        "per_page": per_page,
                    },
                )
            except httpx.HTTPStatusError as exc:
                # 401/403 → likely missing services.read.  Raise a typed
                # exception so callers (CLI vs. auth fallback) can decide
                # whether to fall through or surface the permission error.
                if exc.response.status_code in (401, 403):
                    log.info(
                        "GitHub connection discovery denied (HTTP %d)",
                        exc.response.status_code,
                    )
                    raise GitHubDiscoveryForbidden(
                        exc.response.status_code,
                        "Agent lacks `services.read` permission — cannot "
                        "list GitHub service connections",
                    ) from exc
                raise
            data = resp.json()
            if isinstance(data, dict):
                batch = data.get("data", [])
                meta = data.get("meta") or {}
            else:
                batch = data
                meta = {}
            if not isinstance(batch, list):
                break
            items.extend(batch)

            has_more = meta.get("has_more")
            if has_more is None:
                current = meta.get("current_page")
                last = meta.get("last_page") or meta.get("total_pages")
                if current is not None and last is not None:
                    has_more = current < last
                else:
                    # No usable meta — stop once a page comes back short, which
                    # also covers the empty-page terminator.
                    has_more = len(batch) >= per_page
            if not has_more:
                break
            page += 1
        return items

    async def mint_github_token(
        self,
        service_connection_id: str,
    ) -> dict[str, Any]:
        """``POST /github/installation-token`` — mint a short-lived App token.

        Returns ``{"token": "...", "expires_at": "<iso8601>", ...}``.  The
        broker issues an **installation-wide** token — it does not scope to a
        single repository — so the token grants access to every repo the
        GitHub App installation can reach.  Only works for ``github_app``
        connections; the broker fails closed for PAT-backed
        (``auth_type=token``) connections — those must use the proxy or the
        static ``GITHUB_TOKEN`` fallback.
        """
        body: dict[str, Any] = {"service_connection_id": service_connection_id}
        resp = await self._request(
            "POST", "/api/v1/github/installation-token", json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    # ── Drain mode (graceful shutdown) ────────────────────────────────

    async def enter_drain(
        self,
        *,
        reason: str | None = None,
        deadline_minutes: int | None = None,
    ) -> dict[str, Any]:
        """``POST /agents/drain`` — stop accepting new tasks, finish in-flight."""
        body: dict[str, Any] = {}
        if reason is not None:
            body["reason"] = reason
        if deadline_minutes is not None:
            body["deadline_minutes"] = deadline_minutes
        resp = await self._request(
            "POST", "/api/v1/agents/drain", json=body or None,
        )
        return resp.json()

    async def exit_drain(self) -> dict[str, Any]:
        """``POST /agents/undrain`` — restore normal operation."""
        resp = await self._request("POST", "/api/v1/agents/undrain")
        return resp.json()

    async def drain_status(self) -> dict[str, Any]:
        """``GET /agents/drain`` — current drain state for this agent."""
        resp = await self._request("GET", "/api/v1/agents/drain")
        return resp.json()

    # ── Task tracing / replay ─────────────────────────────────────────

    async def list_tasks(
        self,
        hive_id: str,
        *,
        status: str | None = None,
        type: str | None = None,
        target_agent_id: str | None = None,
        target_capability: str | None = None,
        creator_id: str | None = None,
        parent_task_id: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        q: str | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /tasks`` — paginated list of task summaries with optional filters.

        Filters are AND-combined server-side; only the non-``None`` ones are
        sent as query params. Returns the ``data`` list unwrapped from the
        ``{data, meta, errors}`` envelope (mirrors ``list_dead_letter`` /
        ``list_schedules``). Pagination is controlled via ``page`` / ``per_page``
        (``per_page`` capped at 100 server-side); use ``get_task`` for a single
        task if you need the full ``meta``.
        """
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if type is not None:
            params["type"] = type
        if target_agent_id is not None:
            params["target_agent_id"] = target_agent_id
        if target_capability is not None:
            params["target_capability"] = target_capability
        if creator_id is not None:
            params["creator_id"] = creator_id
        if parent_task_id is not None:
            params["parent_task_id"] = parent_task_id
        if created_after is not None:
            params["created_after"] = created_after
        if created_before is not None:
            params["created_before"] = created_before
        if q is not None:
            params["q"] = q
        if page is not None:
            params["page"] = page
        if per_page is not None:
            params["per_page"] = per_page
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive_id}/tasks", params=params or None,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """``GET /tasks/{task}`` — single task by ID, same shape as claim/complete."""
        hive = self._config.superpos_hive_id
        resp = await self._request("GET", f"/api/v1/hives/{hive}/tasks/{task_id}")
        return resp.json()

    async def update_task(
        self,
        task_id: str,
        *,
        fields: dict[str, Any],
        audit_reason: str | None = None,
        hive_id: str | None = None,
    ) -> dict[str, Any]:
        """``PATCH /tasks/{task}`` — partial update of a not-yet-terminal task.

        ``fields`` is the JSON body sent verbatim; build it from only the
        attributes you want to change so the server's shallow-merge
        semantics apply (an omitted key is left untouched). The mutable
        fields the backend accepts are ``target_agent_id`` (str|null —
        null broadcasts), ``target_capability`` (str|null), ``priority``
        (int 0-4), ``payload`` (object, shallow-merged; a null value
        deletes a key), ``timeout_seconds`` (int), ``max_retries`` (int),
        ``expires_at`` (ISO8601|null) and ``failure_policy`` (object).
        Sending an immutable field returns 422, patching a terminal-state
        task returns 409, and the endpoint is rate-limited to 60/min per
        (hive, task) → 429 — all surface as ``httpx.HTTPStatusError``.

        When ``audit_reason`` is given it is sent as the ``X-Audit-Reason``
        header and recorded verbatim by the backend; it is omitted entirely
        when ``None``. ``hive_id`` overrides the config default for
        cross-hive callers, mirroring the other task methods.
        """
        hive = hive_id if hive_id is not None else self._config.superpos_hive_id
        headers = {"X-Audit-Reason": audit_reason} if audit_reason is not None else None
        resp = await self._request(
            "PATCH",
            f"/api/v1/hives/{hive}/tasks/{task_id}",
            json=fields,
            headers=headers,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_task_trace(self, task_id: str) -> dict[str, Any]:
        """``GET /tasks/{task}/trace`` — full execution trace."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/tasks/{task_id}/trace",
        )
        return resp.json()

    async def replay_task(
        self,
        task_id: str,
        *,
        override_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /tasks/{task}/replay`` — recreate a completed/failed/expired task."""
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {}
        if override_payload is not None:
            body["override_payload"] = override_payload
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/tasks/{task_id}/replay",
            json=body or None,
        )
        return resp.json()

    async def compare_tasks(
        self, task_a: str, task_b: str,
    ) -> dict[str, Any]:
        """``GET /tasks/compare`` — diff two tasks (payload + result + trace)."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/tasks/compare",
            params={"task_a": task_a, "task_b": task_b},
        )
        return resp.json()

    # ── Dead-letter queue ─────────────────────────────────────────────

    async def list_dead_letter(self) -> list[dict[str, Any]]:
        """``GET /tasks/dead-letter`` — list dead-lettered tasks."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/tasks/dead-letter",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_dead_letter(self, task_id: str) -> dict[str, Any]:
        """``GET /tasks/{task}/dead-letter`` — inspect dead-letter detail."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/tasks/{task_id}/dead-letter",
        )
        return resp.json()

    # ── Threads (server-side conversation history) ────────────────────

    async def create_thread(
        self,
        *,
        title: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /threads`` — create a context thread, optionally seeded with a message."""
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if message is not None:
            body["message"] = message
        if metadata is not None:
            body["metadata"] = metadata
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/threads", json=body or None,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def list_threads(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """``GET /threads`` — list context threads in the hive."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/threads", params={"limit": limit},
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def get_thread(self, thread_id: str) -> dict[str, Any]:
        """``GET /threads/{thread}`` — full thread with messages."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "GET", f"/api/v1/hives/{hive}/threads/{thread_id}",
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def append_thread_message(
        self,
        thread_id: str,
        message: str,
        *,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /threads/{thread}/messages`` — append a message."""
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"message": message}
        if task_id is not None:
            body["task_id"] = task_id
        if metadata is not None:
            body["metadata"] = metadata
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/threads/{thread_id}/messages",
            json=body,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def clear_thread_messages(self, thread_id: str) -> dict[str, Any]:
        """``DELETE /threads/{thread}/messages`` — clear messages, keep thread."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "DELETE", f"/api/v1/hives/{hive}/threads/{thread_id}/messages",
        )
        return resp.json()

    async def delete_thread(self, thread_id: str) -> None:
        """``DELETE /threads/{thread}`` — drop thread and all messages."""
        hive = self._config.superpos_hive_id
        await self._request(
            "DELETE", f"/api/v1/hives/{hive}/threads/{thread_id}",
        )

    # ── Schedule pause / resume ───────────────────────────────────────

    async def pause_schedule(self, schedule_id: str) -> dict[str, Any]:
        """``PATCH /schedules/{schedule}/pause`` — halt without deleting."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "PATCH", f"/api/v1/hives/{hive}/schedules/{schedule_id}/pause",
        )
        return resp.json()

    async def resume_schedule(self, schedule_id: str) -> dict[str, Any]:
        """``PATCH /schedules/{schedule}/resume`` — re-arm a paused schedule."""
        hive = self._config.superpos_hive_id
        resp = await self._request(
            "PATCH", f"/api/v1/hives/{hive}/schedules/{schedule_id}/resume",
        )
        return resp.json()

    # ── Events (publish only) ─────────────────────────────────────────

    async def publish_event(
        self,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /events`` — fire an event for server-side subscription fan-out.

        Subscriptions are materialised as tasks server-side, so the
        consumer side needs no separate poll loop — events arrive in the
        normal task queue.  This method exists for the *publish* side.
        """
        hive = self._config.superpos_hive_id
        body: dict[str, Any] = {"type": event_type}
        if payload is not None:
            body["payload"] = payload
        resp = await self._request(
            "POST", f"/api/v1/hives/{hive}/events", json=body,
        )
        return resp.json()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._client.aclose()
