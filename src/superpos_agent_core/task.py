"""Typed ``Task`` resource for the issue #97 SDK/CLI work.

This is the resource-object half of issue #97 (backend: superpos-app
PR #843, ``PATCH /api/v1/hives/{hive}/tasks/{task}``). It pairs with
:meth:`SuperposClient.update_task` and mirrors the existing typed
resource style in this package (:class:`KnowledgeClient` in
``knowledge.py``): a thin wrapper around an existing
:class:`SuperposClient` that reuses that client's HTTP stack verbatim —
the shared ``httpx.AsyncClient``, bearer-token auth, base-url
resolution, the 401 auto-refresh in ``SuperposClient._request``,
``raise_for_status`` error handling, and the ``{data, meta, errors}``
envelope unwrapping. It opens no second connection pool and
re-implements no auth.

The hive id is resolved from the wrapped client's config (matching the
existing task methods on ``SuperposClient``); :meth:`Task.update` also
accepts an optional ``hive`` override for cross-hive callers, mirroring
``KnowledgeClient``'s ``hive``-first signatures.
"""

from __future__ import annotations

from typing import Any

from .superpos_client import SuperposClient


class Task:
    """Typed handle to a single Superpos task.

    Bind it to a ``task_id`` and call :meth:`update` to amend the task —
    chiefly to re-target a misrouted task — without losing its id, parent
    or trace. Parity with the ``.update(**fields)`` ergonomics of the
    other typed resources in this package.
    """

    def __init__(self, client: SuperposClient, task_id: str) -> None:
        self._client = client
        self.task_id = task_id

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Task(task_id={self.task_id!r})"

    async def update(
        self,
        *,
        audit_reason: str | None = None,
        hive: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """``PATCH /tasks/{task}`` — partial update; delegates to
        :meth:`SuperposClient.update_task`.

        Pass only the mutable fields you want to change as keyword
        arguments (``target_agent_id``, ``target_capability``,
        ``priority``, ``payload``, ``timeout_seconds``, ``max_retries``,
        ``expires_at``, ``failure_policy``); they are forwarded verbatim
        as the PATCH body so the server's shallow-merge semantics apply.
        A ``None`` value is sent as JSON ``null`` (e.g.
        ``target_agent_id=None`` broadcasts the task) — it is NOT dropped.

        ``audit_reason`` is sent as the ``X-Audit-Reason`` header when
        given. Raises ``ValueError`` if no fields are supplied so a
        caller mistake fails fast rather than issuing an empty PATCH.
        """
        if not fields:
            raise ValueError("Task.update requires at least one field to change")
        return await self._client.update_task(
            self.task_id,
            fields=fields,
            audit_reason=audit_reason,
            hive_id=hive,
        )
