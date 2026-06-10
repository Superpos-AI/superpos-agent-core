"""Typed knowledge-wiki client for the Phase A3 redesign.

This module is the SDK half of TASK-298 (Knowledge Wiki Redesign,
Phase A3). It pairs with the agent-facing endpoints added in
superpos-app PR #797 (branch ``feat/knowledge-wiki-a3-reads``) and the
A2 write endpoints that already exist:

A3 read endpoints (PR #797)::

    GET  /knowledge/sources            (kind / since / limit filters)
    GET  /knowledge/sources/{id}       (404 if not visible — §6.8 ACL)
    GET  /knowledge/types/{type}/list
    GET  /knowledge/{entry}/backlinks
    POST /knowledge/synthesize-topic   (source_ids / slug → async task)

A2 write endpoints (already live)::

    POST       /knowledge          (create page — dual-shape)
    PUT/PATCH  /knowledge/{entry}   (update page)
    POST       /knowledge/sources   (ingest raw source)

The :class:`KnowledgeClient` wraps an existing :class:`SuperposClient`
so it reuses that client's HTTP stack verbatim: the shared
``httpx.AsyncClient``, bearer-token auth, base-url resolution, the 401
auto-refresh in ``SuperposClient._request``, ``raise_for_status``
error handling, and the ``{data, meta, errors}`` envelope unwrapping.
It does **not** open a second connection pool or re-implement auth.

The hive id is resolved from the wrapped client's config (matching the
existing ``create_knowledge`` / ``list_knowledge`` methods on
``SuperposClient``); every method also accepts an optional ``hive``
override for cross-hive callers, mirroring the ``hive``-first
signatures listed in proposal §8.5.

The legacy ``create_knowledge`` / ``update_knowledge`` /
``get_knowledge`` / ``list_knowledge`` / ``search_knowledge`` methods on
``SuperposClient`` continue to work unchanged and are kept as shims for
one release (proposal §8.5); this module is purely additive.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import httpx

from .superpos_client import SuperposClient

# ---------------------------------------------------------------------------
# Method ↔ endpoint mapping (per proposal §8.4 / §8.5, verified against
# superpos-app PR #797). Methods whose endpoint is NOT live in #797 / A2
# are deferred and raise NotImplementedError rather than 404 a route that
# does not exist:
#
#   live (this module calls them):
#     create_page        -> POST  /knowledge                  (A2, dual-shape)
#     update_page        -> PUT   /knowledge/{entry}          (A2, dual-shape)
#     get_backlinks      -> GET   /knowledge/{entry}/backlinks (#797)
#     list_by_type       -> GET   /knowledge/types/{type}/list (#797)
#     synthesize_topic   -> POST  /knowledge/synthesize-topic  (#797)
#     ingest_source      -> POST  /knowledge/sources           (A2)
#     get_source         -> GET   /knowledge/sources/{id}      (#797)
#     list_sources       -> GET   /knowledge/sources           (#797)
#
#   deferred (endpoint NOT in #797's A3 scope — no live route):
#     get_wiki_index     -> would be GET /knowledge/wiki/index — NOT present
#     get_wiki_log       -> would be GET /knowledge/wiki/log   — NOT present
# ---------------------------------------------------------------------------


def _unwrap(data: Any) -> Any:
    """Unwrap the Superpos ``{data, meta, errors}`` envelope.

    Mirrors the ``data.get("data", data)`` unwrapping the existing
    knowledge methods on :class:`SuperposClient` apply, so a typed
    method returns the same shape callers already expect.
    """
    return data.get("data", data) if isinstance(data, dict) else data


class KnowledgeNotFound(Exception):
    """Raised by :meth:`KnowledgeClient.get_source` on a 404.

    Per proposal §6.8 the source read endpoint collapses "exists but
    not visible to you" and "does not exist" into a single 404 so the
    existence of a hidden source is never leaked. ``get_source`` raises
    this instead of letting the raw ``httpx.HTTPStatusError`` escape so
    callers can distinguish "not visible / gone" from a transport or
    auth failure and decide whether to fall back.
    """

    def __init__(self, source_id: str, message: str | None = None) -> None:
        self.source_id = source_id
        super().__init__(message or f"Knowledge source not found or not visible: {source_id}")


class KnowledgeClient:
    """Typed client for the A3 knowledge-wiki read/write endpoints.

    Wraps a :class:`SuperposClient`; all HTTP goes through the wrapped
    client's ``_request`` so auth, base url, 401 refresh and error
    handling are shared, never duplicated.
    """

    def __init__(self, client: SuperposClient) -> None:
        self._client = client

    # ── helpers ───────────────────────────────────────────────────────

    def _hive(self, hive: str | None) -> str:
        """Resolve the target hive — explicit override or config default."""
        return hive if hive is not None else self._client._config.superpos_hive_id

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue a request through the wrapped client and unwrap the envelope."""
        resp = await self._client._request(method, path, **kwargs)
        return _unwrap(resp.json())

    # ── pages (write — A2, dual-shape) ────────────────────────────────

    async def create_page(
        self,
        *,
        type: str,
        slug: str,
        body: str,
        frontmatter: Mapping[str, Any] | None = None,
        source_ids: Sequence[str] | None = None,
        sources: Sequence[Mapping[str, Any]] | None = None,
        title: str | None = None,
        summary: str | None = None,
        tags: Sequence[str] | None = None,
        scope: str = "hive",
        visibility: str = "public",
        ttl: str | None = None,
        hive: str | None = None,
    ) -> dict[str, Any]:
        """``POST /knowledge`` — create a typed wiki page (new shape).

        ``sources=`` is the transactional ingest-and-attach list
        (§6.8 / §8.1): each descriptor is ingested **and** attached to
        this page in the same write, satisfying attach rule (a). Each
        descriptor matches the ingest-source contract — ``kind``,
        ``uri``, ``content_sha256`` (required) plus optional ``title``,
        ``raw_excerpt``, ``metadata``, ``origin``.

        ``source_ids=`` attaches already-ingested sources; each id is
        subject to the §6.8 attach-time authorization rule (a source
        the caller cannot already see returns 403 and rolls back).

        ``summary=`` is the page's top-level one-line summary (max 500
        chars server-side); it is sent as a top-level field, never folded
        into ``frontmatter``.

        ``ttl=`` is an optional ISO8601 expiry timestamp after which the
        entry auto-expires; like ``summary`` it is sent as a top-level
        field (omitted entirely when ``None``).
        """
        hive_id = self._hive(hive)
        payload: dict[str, Any] = {
            "type": type,
            "slug": slug,
            "body": body,
            "scope": scope,
            "visibility": visibility,
        }
        if frontmatter is not None:
            payload["frontmatter"] = dict(frontmatter)
        if source_ids is not None:
            payload["source_ids"] = list(source_ids)
        if sources is not None:
            payload["sources"] = [dict(s) for s in sources]
        if title is not None:
            payload["title"] = title
        if summary is not None:
            payload["summary"] = summary
        if tags is not None:
            payload["tags"] = list(tags)
        if ttl is not None:
            payload["ttl"] = ttl
        return await self._request_json(
            "POST", f"/api/v1/hives/{hive_id}/knowledge", json=payload,
        )

    async def update_page(
        self,
        entry_id: str,
        *,
        body: str | None = None,
        frontmatter: Mapping[str, Any] | None = None,
        title: str | None = None,
        summary: str | None = None,
        tags: Sequence[str] | None = None,
        source_ids: Sequence[str] | None = None,
        sources: Sequence[Mapping[str, Any]] | None = None,
        visibility: str | None = None,
        ttl: str | None = None,
        hive: str | None = None,
    ) -> dict[str, Any]:
        """``PUT /knowledge/{entry}`` — update a typed wiki page.

        Supports partial updates and bumps the page version server-side.
        Only the fields you pass are sent, so an update that touches just
        ``frontmatter`` leaves ``body`` untouched. ``body`` is a full
        replacement of the page body. ``summary`` is the top-level
        one-line summary (max 500 chars), sent as a top-level field.
        ``ttl`` is an optional ISO8601 expiry timestamp, also sent as a
        top-level field (omitted when ``None``). ``scope`` is immutable
        post-create and is not accepted here.
        """
        hive_id = self._hive(hive)
        payload: dict[str, Any] = {}
        if body is not None:
            payload["body"] = body
        if frontmatter is not None:
            payload["frontmatter"] = dict(frontmatter)
        if title is not None:
            payload["title"] = title
        if summary is not None:
            payload["summary"] = summary
        if tags is not None:
            payload["tags"] = list(tags)
        if source_ids is not None:
            payload["source_ids"] = list(source_ids)
        if sources is not None:
            payload["sources"] = [dict(s) for s in sources]
        if visibility is not None:
            payload["visibility"] = visibility
        if ttl is not None:
            payload["ttl"] = ttl
        return await self._request_json(
            "PUT", f"/api/v1/hives/{hive_id}/knowledge/{entry_id}", json=payload,
        )

    # ── pages (read — A3) ─────────────────────────────────────────────

    async def get_backlinks(
        self, entry_id: str, *, hive: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /knowledge/{entry}/backlinks`` — incoming ``[[…]]`` links.

        Returns the pages that link TO this entry via ``wiki_links``.
        Scope visibility is applied server-side, so a backlinking page
        the caller cannot read is excluded.
        """
        hive_id = self._hive(hive)
        return await self._request_json(
            "GET", f"/api/v1/hives/{hive_id}/knowledge/{entry_id}/backlinks",
        )

    async def list_by_type(
        self,
        type: str,
        *,
        limit: int = 50,
        scope: str | None = None,
        hive: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /knowledge/types/{type}/list`` — pages of a type, newest first.

        ``type`` is validated server-side against the frontmatter type
        set; an unknown type returns 422 (surfaced as
        ``httpx.HTTPStatusError``) rather than a silent empty list.
        ``limit`` is clamped 1..100 server-side.
        """
        hive_id = self._hive(hive)
        params: dict[str, Any] = {"limit": limit}
        if scope is not None:
            params["scope"] = scope
        return await self._request_json(
            "GET", f"/api/v1/hives/{hive_id}/knowledge/types/{type}/list",
            params=params,
        )

    async def synthesize_topic(
        self,
        source_ids: Sequence[str],
        *,
        slug: str | None = None,
        hive: str | None = None,
    ) -> dict[str, Any]:
        """``POST /knowledge/synthesize-topic`` — dispatch async synthesis.

        Given a non-empty list of source ULIDs, enqueues a
        ``knowledge.synthesize_topic`` task that writes a new ``topic:``
        page and emits a ``log:`` entry. The §6.8 read ACL is enforced
        on every supplied source: seeding from a source the caller
        cannot read returns 403. Returns the created task descriptor
        (``task_id``, ``status``, …) so the caller can poll for
        completion.
        """
        hive_id = self._hive(hive)
        payload: dict[str, Any] = {"source_ids": list(source_ids)}
        if slug is not None:
            payload["slug"] = slug
        return await self._request_json(
            "POST", f"/api/v1/hives/{hive_id}/knowledge/synthesize-topic",
            json=payload,
        )

    # ── sources ───────────────────────────────────────────────────────

    async def ingest_source(
        self,
        *,
        kind: str,
        uri: str,
        content_sha256: str,
        title: str | None = None,
        raw_excerpt: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        origin: str | None = None,
        hive: str | None = None,
    ) -> dict[str, Any]:
        """``POST /knowledge/sources`` — ingest a raw source (A2).

        Idempotent on ``(organization_id, content_sha256, kind,
        origin)`` — the server returns the existing row on a dedupe
        hit, so ingest → ``create_page(source_ids=[…])`` chains safely.
        ``content_sha256`` is required by the endpoint (SHA-256 hex of
        the source content); ``origin`` is ``"hive"`` or ``"org"``.

        The returned source begins as an **orphan** (zero citing
        pages); its originator may later attach it to a page under the
        §6.8 attach rule. For a single-call ingest-and-attach, pass the
        descriptor inline via :meth:`create_page` ``sources=[…]``.
        """
        hive_id = self._hive(hive)
        payload: dict[str, Any] = {
            "kind": kind,
            "uri": uri,
            "content_sha256": content_sha256,
        }
        if title is not None:
            payload["title"] = title
        if raw_excerpt is not None:
            payload["raw_excerpt"] = raw_excerpt
        if metadata is not None:
            payload["metadata"] = dict(metadata)
        if origin is not None:
            payload["origin"] = origin
        return await self._request_json(
            "POST", f"/api/v1/hives/{hive_id}/knowledge/sources", json=payload,
        )

    async def get_source(
        self, source_id: str, *, hive: str | None = None,
    ) -> dict[str, Any]:
        """``GET /knowledge/sources/{id}`` — fetch one raw source.

        Succeeds only if the caller can read a page whose
        ``source_ids`` contains ``source_id`` (§6.8); otherwise the
        endpoint returns 404 (the same not-found shape as a hidden
        page, so existence is never leaked). On that 404 this method
        raises :class:`KnowledgeNotFound` rather than letting the raw
        ``httpx.HTTPStatusError`` escape, so callers can cleanly
        distinguish "not visible / gone" from a transport error. Any
        other non-2xx still propagates as ``httpx.HTTPStatusError``.
        """
        hive_id = self._hive(hive)
        try:
            return await self._request_json(
                "GET", f"/api/v1/hives/{hive_id}/knowledge/sources/{source_id}",
            )
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise KnowledgeNotFound(source_id) from exc
            raise

    async def list_sources(
        self,
        *,
        kind: str | None = None,
        since: str | None = None,
        limit: int = 50,
        hive: str | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /knowledge/sources`` — list sources visible to the caller.

        Only sources cited by at least one page the caller can read
        (§6.8 derived visibility) are returned; orphan sources and
        sources cited only by hidden pages never appear. ``kind``
        filters by source kind, ``since`` is an ISO-8601 lower bound on
        ``captured_at``, ``limit`` is clamped 1..100 server-side.
        """
        hive_id = self._hive(hive)
        params: dict[str, Any] = {"limit": limit}
        if kind is not None:
            params["kind"] = kind
        if since is not None:
            params["since"] = since
        return await self._request_json(
            "GET", f"/api/v1/hives/{hive_id}/knowledge/sources", params=params,
        )

    # ── deferred — endpoints NOT live in PR #797's A3 scope ───────────

    async def get_wiki_index(
        self, *, scope: str = "hive", hive: str | None = None,
    ) -> dict[str, Any]:
        """Deferred: the in-wiki ``index`` page endpoint is not live yet.

        Proposal §8.5 lists ``get_wiki_index`` but PR #797 ships **no**
        ``/knowledge/wiki/index`` (or equivalent) route — the in-wiki
        ``index.md`` is part of the bookkeeper rewrite (§7.3 / Phase B),
        not the A3 read scope. Calling a non-existent route would 404,
        so this method raises instead of guessing a URL. Until the
        endpoint lands, list pages with :meth:`list_by_type`.
        """
        raise NotImplementedError(
            "get_wiki_index is deferred: no /knowledge/wiki/index endpoint "
            "exists in PR #797 (A3). The in-wiki index page is part of the "
            "bookkeeper rewrite (proposal §7.3 / Phase B). Use list_by_type() "
            "in the meantime.",
        )

    async def get_wiki_log(
        self, *, since: str | None = None, hive: str | None = None,
    ) -> dict[str, Any]:
        """Deferred: the in-wiki ``log`` page endpoint is not live yet.

        Proposal §8.5 lists ``get_wiki_log`` but PR #797 ships **no**
        ``/knowledge/wiki/log`` route — the in-wiki ``log.md`` is part
        of the bookkeeper rewrite (§7.3 / Phase B), not the A3 read
        scope. This method raises rather than calling a route that
        would 404.
        """
        raise NotImplementedError(
            "get_wiki_log is deferred: no /knowledge/wiki/log endpoint "
            "exists in PR #797 (A3). The in-wiki log page is part of the "
            "bookkeeper rewrite (proposal §7.3 / Phase B).",
        )
