"""CLI helper for creating Superpos tasks and schedules from within an agent container.

Mounted on PATH inside each agent's Docker image so the LLM can shell out
to ``superpos-task create --prompt …`` etc. when it needs to spawn a subtask.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def _base_config() -> tuple[str, str, str, str]:
    base_url = os.environ.get("SUPERPOS_BASE_URL", "").rstrip("/")
    hive_id = os.environ.get("SUPERPOS_HIVE_ID", "")
    agent_id = os.environ.get("SUPERPOS_AGENT_ID", "")
    token = os.environ.get("SUPERPOS_API_TOKEN", "")

    if not base_url or not hive_id or not token:
        print(
            "Error: SUPERPOS_BASE_URL, SUPERPOS_HIVE_ID, and SUPERPOS_API_TOKEN must be set",
            file=sys.stderr,
        )
        sys.exit(1)

    return base_url, hive_id, agent_id, token


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def create_task(
    prompt: str,
    task_type: str = "default",
    capability: str | None = None,
    priority: int = 2,
    self_target: bool = False,
    payload_extra: dict | None = None,
    timeout_seconds: int = 1800,
) -> None:
    """Create a task in Superpos.

    By default the task is broadcast (no ``target_agent_id``) so any agent
    with the right capability can claim it. Pass ``self_target=True`` to
    pin the task to the calling agent — useful for self-routed follow-up
    work, but the rare case in a multi-agent hive.
    """
    base_url, hive_id, agent_id, token = _base_config()

    body: dict = {
        "type": task_type,
        "payload": {"prompt": prompt, **(payload_extra or {})},
        "priority": priority,
        "timeout_seconds": timeout_seconds,
    }

    if self_target and agent_id:
        body["target_agent_id"] = agent_id
    if capability:
        body["target_capability"] = capability

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.post(
            f"/api/v1/hives/{hive_id}/tasks",
            json=body,
            headers=_headers(token),
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            task_data = data.get("data", data)
            task_id = task_data.get("id", "unknown") if isinstance(task_data, dict) else "unknown"
            print(f"Task created: {task_id}")
        else:
            print(f"Error creating task: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)


def create_schedule(
    name: str,
    trigger_type: str,
    task_type: str = "default",
    prompt: str | None = None,
    cron_expression: str | None = None,
    interval_seconds: int | None = None,
    run_at: str | None = None,
    self_target: bool = False,
    overlap_policy: str = "skip",
) -> None:
    """Create a task schedule in Superpos."""
    base_url, hive_id, agent_id, token = _base_config()

    body: dict = {
        "name": name,
        "trigger_type": trigger_type,
        "task_type": task_type,
        "overlap_policy": overlap_policy,
    }

    if prompt:
        body["task_payload"] = {"prompt": prompt}
    if cron_expression:
        body["cron_expression"] = cron_expression
    if interval_seconds is not None:
        body["interval_seconds"] = interval_seconds
    if run_at:
        body["run_at"] = run_at
    if self_target and agent_id:
        body["task_target_agent_id"] = agent_id

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.post(
            f"/api/v1/hives/{hive_id}/schedules",
            json=body,
            headers=_headers(token),
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            sched_data = data.get("data", data)
            sched_id = sched_data.get("id", "unknown") if isinstance(sched_data, dict) else "unknown"
            print(f"Schedule created: {sched_id}")
        else:
            print(f"Error creating schedule: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)


def list_schedules() -> None:
    """List all schedules in the hive."""
    base_url, hive_id, _, token = _base_config()

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(
            f"/api/v1/hives/{hive_id}/schedules",
            headers=_headers(token),
        )
        if resp.status_code == 200:
            data = resp.json()
            schedules = data.get("data", [])
            if not schedules:
                print("No schedules found.")
                return
            for s in schedules:
                status = s.get("status", "?")
                trigger = s.get("trigger_type", "?")
                name = s.get("name", "unnamed")
                sid = s.get("id", "?")
                next_run = s.get("next_run_at", "n/a")
                print(f"  [{status}] {sid}  {name}  ({trigger}, next: {next_run})")
        else:
            print(f"Error listing schedules: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)


def delete_schedule(schedule_id: str) -> None:
    """Delete a schedule."""
    base_url, hive_id, _, token = _base_config()

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.delete(
            f"/api/v1/hives/{hive_id}/schedules/{schedule_id}",
            headers=_headers(token),
        )
        if resp.status_code in (200, 204):
            print(f"Schedule {schedule_id} deleted.")
        else:
            print(f"Error deleting schedule: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)


def _parse_json_object(raw: str, flag: str) -> dict:
    """Parse a CLI JSON-object argument, erroring cleanly on bad input."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: {flag} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(value, dict):
        print(f"Error: {flag} must be a JSON object, got {type(value).__name__}", file=sys.stderr)
        sys.exit(1)
    return value


def _build_update_fields(args: argparse.Namespace) -> dict:
    """Build the PATCH body from ONLY the flags the caller actually passed.

    argparse leaves unspecified options at their ``None`` default, so an
    omitted flag is dropped from the body entirely (preserving the
    server's shallow-merge: untouched attributes stay put). For the
    nullable string fields a flag passed with an empty value (``''``) is
    sent as JSON ``null`` — that is how ``--target-agent-id ''`` becomes a
    broadcast and ``--expires-at ''`` clears the expiry.
    """
    fields: dict = {}

    # Nullable strings: '' → null, any other value → the string, omit if not passed.
    for flag_attr, key in (
        ("target_agent_id", "target_agent_id"),
        ("target_capability", "target_capability"),
        ("expires_at", "expires_at"),
    ):
        val = getattr(args, flag_attr)
        if val is not None:
            fields[key] = None if val == "" else val

    if args.priority is not None:
        fields["priority"] = args.priority
    if args.timeout_seconds is not None:
        fields["timeout_seconds"] = args.timeout_seconds
    if args.max_retries is not None:
        fields["max_retries"] = args.max_retries
    if args.payload is not None:
        fields["payload"] = _parse_json_object(args.payload, "--payload")
    if args.failure_policy is not None:
        fields["failure_policy"] = _parse_json_object(args.failure_policy, "--failure-policy")

    return fields


def update_task(task_id: str, fields: dict, audit_reason: str | None = None) -> None:
    """Partially update a task via ``PATCH /tasks/{task}`` (issue #97).

    ``fields`` must already be built from ONLY the flags the caller
    actually passed, so the backend's shallow-merge semantics apply: an
    omitted attribute is left untouched, while an explicit ``None`` value
    (e.g. ``target_agent_id=None``) is sent as JSON ``null`` to broadcast
    the task / clear the field. ``audit_reason`` is sent as the
    ``X-Audit-Reason`` header when provided.
    """
    base_url, hive_id, _, token = _base_config()

    headers = _headers(token)
    if audit_reason is not None:
        headers["X-Audit-Reason"] = audit_reason

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.patch(
            f"/api/v1/hives/{hive_id}/tasks/{task_id}",
            json=fields,
            headers=headers,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            task_data = data.get("data", data) if isinstance(data, dict) else data
            print(json.dumps(task_data, indent=2, sort_keys=True))
            return
        if resp.status_code == 422:
            print(
                f"Error: invalid update (422) — an immutable or invalid field was "
                f"sent: {resp.text}",
                file=sys.stderr,
            )
        elif resp.status_code == 409:
            print(
                f"Error: task is in a terminal state and can no longer be updated "
                f"(409): {resp.text}",
                file=sys.stderr,
            )
        elif resp.status_code == 429:
            print(
                f"Error: rate limited (429) — too many updates to this task; "
                f"retry shortly: {resp.text}",
                file=sys.stderr,
            )
        else:
            print(f"Error updating task: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)


def update_memory(content: str, message: str | None = None, mode: str = "append") -> None:
    """Update the MEMORY document in the active persona."""
    base_url = os.environ.get("SUPERPOS_BASE_URL", "").rstrip("/")
    token = os.environ.get("SUPERPOS_API_TOKEN", "")

    if not base_url or not token:
        print("Error: SUPERPOS_BASE_URL and SUPERPOS_API_TOKEN must be set", file=sys.stderr)
        sys.exit(1)

    body: dict = {"content": content, "mode": mode}
    if message:
        body["message"] = message

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.patch(
            "/api/v1/persona/memory",
            json=body,
            headers=_headers(token),
        )
        if resp.status_code in (200, 201):
            print("Memory updated.")
        elif resp.status_code == 403:
            print("Error: MEMORY document is locked by persona lock policy", file=sys.stderr)
            sys.exit(1)
        elif resp.status_code == 409:
            print("Error: Persona version conflict — retry", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Error: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Superpos task & schedule helper")
    sub = parser.add_subparsers(dest="command")

    create = sub.add_parser("create", help="Create a new task")
    create.add_argument("--prompt", required=True, help="Task prompt / instructions")
    create.add_argument("--type", default="default", help="Task type (default: 'default')")
    create.add_argument("--capability", help="Required agent capability")
    create.add_argument("--priority", type=int, default=2, help="Priority 0-4 (default: 2)")
    create.add_argument("--timeout", type=int, default=1800, help="Claim timeout in seconds (default: 1800)")
    create.add_argument("--self-target", action="store_true", help="Pin the task to this agent (default: broadcast)")

    sched_create = sub.add_parser("schedule", help="Create a schedule")
    sched_create.add_argument("--name", required=True, help="Schedule name")
    sched_create.add_argument(
        "--trigger", required=True, choices=["once", "interval", "cron"],
        help="Trigger type",
    )
    sched_create.add_argument("--prompt", help="Task prompt")
    sched_create.add_argument("--task-type", default="default", help="Task type")
    sched_create.add_argument("--cron", help="Cron expression (for trigger=cron)")
    sched_create.add_argument("--interval", type=int, help="Interval in seconds")
    sched_create.add_argument("--run-at", help="ISO8601 datetime (for trigger=once)")
    sched_create.add_argument("--overlap", default="skip", choices=["skip", "allow", "cancel_previous"])
    sched_create.add_argument("--self-target", action="store_true")

    update = sub.add_parser(
        "update",
        help="Partially update a task (re-target, re-prioritise, etc.)",
    )
    update.add_argument("task_id", help="ID of the task to update")
    update.add_argument(
        "--target-agent-id",
        help="Pin the task to an agent; pass '' to clear (broadcast)",
    )
    update.add_argument(
        "--target-capability",
        help="Route by capability; pass '' to clear",
    )
    update.add_argument("--priority", type=int, help="Priority 0-4")
    update.add_argument("--payload", help="JSON object, shallow-merged into payload")
    update.add_argument("--timeout-seconds", type=int, help="Claim timeout in seconds")
    update.add_argument("--max-retries", type=int, help="Max retry attempts")
    update.add_argument(
        "--expires-at",
        help="ISO8601 expiry; pass '' to clear",
    )
    update.add_argument("--failure-policy", help="JSON object")
    update.add_argument(
        "--audit-reason",
        help="Reason recorded in the audit log (recommended)",
    )

    sub.add_parser("schedules", help="List schedules")

    sched_del = sub.add_parser("delete-schedule", help="Delete a schedule")
    sched_del.add_argument("--id", required=True, help="Schedule ID")

    mem = sub.add_parser("memory", help="Update the MEMORY document in the active persona")
    mem.add_argument("--content", required=True, help="Content to write (Markdown)")
    mem.add_argument("--message", help="Optional changelog message")
    mem.add_argument(
        "--mode", default="append", choices=["append", "prepend", "replace"],
        help="Write mode (default: append)",
    )

    args = parser.parse_args()

    if args.command == "create":
        create_task(
            prompt=args.prompt,
            task_type=args.type,
            capability=args.capability,
            priority=args.priority,
            self_target=args.self_target,
            timeout_seconds=args.timeout,
        )
    elif args.command == "schedule":
        create_schedule(
            name=args.name,
            trigger_type=args.trigger,
            task_type=args.task_type,
            prompt=args.prompt,
            cron_expression=args.cron,
            interval_seconds=args.interval,
            run_at=args.run_at,
            self_target=args.self_target,
            overlap_policy=args.overlap,
        )
    elif args.command == "update":
        fields = _build_update_fields(args)
        if not fields:
            print(
                "Error: no fields to update — pass at least one of "
                "--target-agent-id, --target-capability, --priority, --payload, "
                "--timeout-seconds, --max-retries, --expires-at, --failure-policy.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not args.audit_reason:
            print(
                "Warning: no --audit-reason given; an audit reason is strongly "
                "recommended for task updates.",
                file=sys.stderr,
            )
        update_task(args.task_id, fields, audit_reason=args.audit_reason)
    elif args.command == "schedules":
        list_schedules()
    elif args.command == "delete-schedule":
        delete_schedule(args.id)
    elif args.command == "memory":
        update_memory(content=args.content, message=args.message, mode=args.mode)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
