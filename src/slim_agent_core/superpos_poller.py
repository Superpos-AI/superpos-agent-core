"""Superpos polling daemon — polls for tasks and enqueues them on the agent's executor."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from .config import BaseConfig
from .executor import Executor, ExecutionRequest
from .superpos_client import SuperposClient
from .worktree_manager import infer_branch

log = logging.getLogger(__name__)

# Cooldown (seconds) for deduplicating webhook events on the same entity.
# Multiple webhook events for the same repo+PR/issue within this window
# are auto-completed — only the first triggers an execution.
WEBHOOK_ENTITY_COOLDOWN = 300

# Maximum number of times a task can be re-claimed after claim expiry.
# After this limit the task is left for the server to handle (timeout/fail).
MAX_TASK_CLAIMS = 3


def _webhook_entity_key(task: dict) -> str | None:
    """Extract dedup key for a webhook task (e.g. 'owner/repo:pr:123').

    Covers multiple GitHub event types:
    - pull_request / pull_request_review / pull_request_review_comment → pr number
    - issues / issue_comment → issue number
    - push → repo:push:branch (bot's own commits trigger these)
    - check_run / check_suite → nested pull_requests array
    - no fallback for unknown event types (avoids false-positive dedup across PRs)
    """
    payload = task.get("payload", {}) or {}
    event_payload = payload.get("event_payload") if isinstance(payload, dict) else None
    if not isinstance(event_payload, dict):
        return None
    repo = (event_payload.get("repository") or {}).get("full_name")
    if not repo:
        return None

    pr = event_payload.get("pull_request") or {}
    if isinstance(pr, dict) and pr.get("number"):
        return f"{repo}:pr:{pr['number']}"

    issue = event_payload.get("issue") or {}
    if isinstance(issue, dict) and issue.get("number"):
        return f"{repo}:issue:{issue['number']}"

    ref = event_payload.get("ref")
    if ref and isinstance(ref, str):
        branch = ref.removeprefix("refs/heads/")
        return f"{repo}:push:{branch}"

    for key in ("check_run", "check_suite"):
        check = event_payload.get(key)
        if isinstance(check, dict):
            prs = check.get("pull_requests") or []
            if isinstance(prs, list) and prs:
                pr_num = prs[0].get("number")
                if pr_num:
                    return f"{repo}:pr:{pr_num}"

    return None


async def run_superpos_poller(
    superpos: SuperposClient,
    executor: Executor,
    config: BaseConfig,
) -> None:
    """Poll Superpos for tasks and enqueue them for execution.

    Heartbeat is sent on every poll iteration so the agent stays online
    in the Superpos dashboard without a separate background loop.
    """
    log.info("Superpos poller started (interval=%ds)", config.superpos_poll_interval)

    persona_version: int | None = None
    platform_context_version: int | None = None
    environment_version: str | None = None
    recent_webhook_entities: dict[str, tuple[float, str]] = {}
    task_claim_counts: dict[str, int] = {}
    _failed_tasks: set[str] = set()

    try:
        while True:
            try:
                await superpos.heartbeat()
            except Exception:
                log.exception("Heartbeat failed")

            try:
                ver_data = await superpos.get_persona_version(
                    known_version=persona_version,
                    known_platform_version=platform_context_version,
                    known_environment_version=environment_version,
                )
                data = ver_data.get("data", ver_data) if isinstance(ver_data, dict) else {}
                changed = data.get("changed", False)
                server_version = data.get("version")
                server_platform_version = data.get("platform_context_version")
                server_environment_version = data.get("environment_version")

                # Refetch the assembled prompt whenever ANY dimension changed —
                # persona, platform context, or live hive environment.  The
                # server's `changed` flag already factors all three in when the
                # client passed `known_*` for each.
                if changed or (
                    server_version is not None and server_version != persona_version
                ):
                    new_persona = await superpos.get_persona_assembled()
                    executor.update_persona(new_persona, version=server_version)
                    persona_version = server_version
                    if server_platform_version is not None:
                        platform_context_version = server_platform_version
                    if server_environment_version is not None:
                        environment_version = server_environment_version
                    log.info(
                        "Persona refreshed (version=%s, platform=%s, env=%s)",
                        persona_version, platform_context_version, environment_version,
                    )
                else:
                    # Seed local tracking for first-run / pre-existing state so
                    # subsequent polls correctly compare known→server values.
                    if persona_version is None and server_version is not None:
                        persona_version = server_version
                    if platform_context_version is None and server_platform_version is not None:
                        platform_context_version = server_platform_version
                    if environment_version is None and server_environment_version is not None:
                        environment_version = server_environment_version
            except Exception:
                log.debug("Persona version check failed", exc_info=True)

            try:
                tasks = await superpos.poll_tasks()

                _now = time.monotonic()
                recent_webhook_entities = {
                    k: v for k, v in recent_webhook_entities.items()
                    if _now - v[0] < WEBHOOK_ENTITY_COOLDOWN
                }

                for task in tasks:
                    task_id = str(task.get("id", ""))

                    if task.get("type") == "webhook_handler" and task_id:
                        entity_key = _webhook_entity_key(task)
                        if entity_key and entity_key in recent_webhook_entities:
                            _, primary_id = recent_webhook_entities[entity_key]
                            try:
                                await superpos.claim_task(task_id)
                                await superpos.complete_task(
                                    task_id,
                                    f"Consolidated: duplicate webhook for {entity_key}, "
                                    f"already handled by task {primary_id}.",
                                )
                                log.info(
                                    "Auto-completed duplicate webhook task %s (entity=%s)",
                                    task_id, entity_key,
                                )
                            except Exception:
                                log.debug("Failed to auto-complete duplicate webhook %s", task_id)
                            continue

                    payload = task.get("payload", {}) or {}
                    invoke = task.get("invoke", {}) or {}
                    prompt = ""

                    if isinstance(invoke, dict):
                        prompt = invoke.get("instructions", "")
                    if not prompt and isinstance(payload, dict):
                        prompt = payload.get("prompt", payload.get("input", ""))
                    if not prompt:
                        prompt = task.get("input", task.get("prompt", task.get("description", "")))

                    if not prompt and task.get("type") == "webhook_handler":
                        event_payload = (
                            payload.get("event_payload") if isinstance(payload, dict) else None
                        )
                        if isinstance(event_payload, dict):
                            action = event_payload.get("action", "unknown")
                            repo = (event_payload.get("repository") or {}).get("full_name", "unknown")
                            prompt = (
                                f"Handle this GitHub webhook event: action={action}, "
                                f"repo={repo}. Inspect the attached payload for full details."
                            )

                    context_data = task.get("payload") or task.get("event_payload")
                    if not context_data:
                        context_data = (
                            payload.get("event_payload") if isinstance(payload, dict) else None
                        )
                    if context_data and prompt:
                        context_json = json.dumps(
                            context_data, indent=2, ensure_ascii=False, default=str,
                        )
                        # Cap payload size to avoid "Argument list too long" when
                        # the agent CLI receives the full prompt as a CLI arg.
                        max_payload = 50_000
                        if len(context_json) > max_payload:
                            context_json = context_json[:max_payload] + "\n... (truncated)"
                        prompt = (
                            f"{prompt}\n\n---\n\n"
                            f"**Task payload data:**\n```json\n{context_json}\n```"
                        )

                    if not task_id or not prompt:
                        log.warning("Skipping task with missing id/prompt: %s", task)
                        continue

                    if executor.has_superpos_task(task_id):
                        log.debug("Skipping already in-flight task %s", task_id)
                        continue

                    # Stop re-claiming tasks that keep expiring — prevents
                    # infinite claim-expire-reclaim loops.  Claim + fail the
                    # task so the server removes it from the pending queue.
                    prior_claims = task_claim_counts.get(task_id, 0)
                    if prior_claims >= MAX_TASK_CLAIMS:
                        if task_id not in _failed_tasks:
                            log.warning(
                                "Task %s claimed %d times — failing on server",
                                task_id, prior_claims,
                            )
                            try:
                                await superpos.claim_task(task_id)
                                await superpos.fail_task(
                                    task_id,
                                    f"Agent gave up after {prior_claims} claim attempts "
                                    f"(claims kept expiring).",
                                )
                            except Exception:
                                log.debug("Failed to fail zombie task %s", task_id)
                            _failed_tasks.add(task_id)
                        continue

                    # Background tasks (dream, knowledge_fillin) bypass the
                    # Telegram streamer and the semaphore — they're internal
                    # housekeeping, not user-facing work.
                    task_type = task.get("type")
                    is_background = task_type in ("dream", "knowledge_fillin")

                    if not is_background and not executor.has_free_slots:
                        log.debug(
                            "Executor at capacity (%d slots), deferring remaining tasks",
                            config.executor_max_parallel,
                        )
                        break

                    try:
                        await superpos.claim_task(task_id)
                    except Exception:
                        log.warning("Failed to claim task %s (maybe already claimed)", task_id)
                        continue

                    task_claim_counts[task_id] = prior_claims + 1

                    if is_background:
                        raw_timeout = task.get("timeout_seconds")
                        try:
                            timeout_s = int(raw_timeout) if raw_timeout else 300
                        except (TypeError, ValueError):
                            timeout_s = 300
                        asyncio.create_task(
                            executor.run_background(task_id, prompt, task_type, timeout_s)
                        )
                        log.info(
                            "%s task %s started in background (timeout=%ds)",
                            task_type.replace("_", " ").capitalize(), task_id, timeout_s,
                        )
                        continue

                    executor.add_superpos_task(task_id)

                    chat_id = config.telegram_chat_id
                    if not chat_id:
                        log.warning("No TELEGRAM_CHAT_ID set, skipping Superpos task notification")
                        chat_id = "0"

                    branch = infer_branch(task)
                    if branch:
                        log.debug("Inferred branch %r for task %s", branch, task_id)

                    if task.get("type") == "webhook_handler":
                        ek = _webhook_entity_key(task)
                        if ek:
                            recent_webhook_entities[ek] = (time.monotonic(), task_id)

                    req = ExecutionRequest(
                        prompt=prompt,
                        chat_id=chat_id,
                        source="superpos",
                        superpos_task_id=task_id,
                        branch=branch,
                    )
                    await executor.queue.put(req)
                    log.info("Enqueued superpos task %s (queue=%d)", task_id, executor.pending)

            except Exception:
                log.exception("Superpos poll error")

            await asyncio.sleep(config.superpos_poll_interval)

    except asyncio.CancelledError:
        log.info("Superpos poller shutting down")
        raise
