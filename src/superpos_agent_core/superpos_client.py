"""Thin async HTTP client for the Superpos REST API."""

from __future__ import annotations

import logging
from typing import Any

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
        """Make a request, auto-refreshing token on 401."""
        resp = await self._client.request(method, path, headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            log.warning("Superpos 401 — attempting token refresh")
            if await self.refresh_auth():
                resp = await self._client.request(
                    method, path, headers=self._headers(), **kwargs
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

    async def heartbeat(self) -> None:
        await self._request("POST", "/api/v1/agents/heartbeat")

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
        semantic: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """``GET /knowledge/search`` — PostgreSQL FTS or pgvector semantic search.

        The server requires at least one of ``q`` or ``scope`` and returns
        400 otherwise; we raise ``ValueError`` here instead so a caller
        mistake fails fast and synchronously rather than as a delayed
        ``httpx.HTTPStatusError`` from the network.  ``semantic=True``
        routes to pgvector cosine-similarity (embedding-backed); the
        default ``semantic=False`` uses Postgres ``ts_query`` / ``ts_rank``
        with highlighted snippets.

        Returns the unwrapped entry list — pagination meta (``total``,
        ``query``) is on the envelope, callers needing it should hit the
        raw endpoint via ``_request`` directly.
        """
        if q is None and scope is None:
            raise ValueError(
                "search_knowledge requires at least one of `q` or `scope`",
            )
        hive = self._config.superpos_hive_id
        params: dict[str, Any] = {"limit": limit}
        if q is not None:
            params["q"] = q
        if scope is not None:
            params["scope"] = scope
        if semantic:
            params["semantic"] = "true"
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
            merged = self._headers()
            merged.update(headers)
            kwargs["headers"] = merged
        return await self._request(method, endpoint, **kwargs)

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

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """``GET /tasks/{task}`` — single task by ID, same shape as claim/complete."""
        hive = self._config.superpos_hive_id
        resp = await self._request("GET", f"/api/v1/hives/{hive}/tasks/{task_id}")
        return resp.json()

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
