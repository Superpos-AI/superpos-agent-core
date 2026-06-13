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


def list_tasks(
    status: str | None = None,
    task_type: str | None = None,
    target_agent_id: str | None = None,
    target_capability: str | None = None,
    creator_id: str | None = None,
    parent_task_id: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    q: str | None = None,
    page: int | None = None,
    per_page: int | None = None,
    as_json: bool = False,
) -> None:
    """List tasks in the hive, with optional filters (AND-combined server-side).

    Only the filters that are set are sent as query params. Prints a compact
    human-readable summary by default, or the raw ``data`` list as JSON with
    ``--json``.
    """
    base_url, hive_id, _, token = _base_config()

    params: dict = {}
    if status is not None:
        params["status"] = status
    if task_type is not None:
        params["type"] = task_type
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

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(
            f"/api/v1/hives/{hive_id}/tasks",
            params=params or None,
            headers=_headers(token),
        )
        if resp.status_code != 200:
            print(f"Error listing tasks: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)
        data = resp.json()
        tasks = data.get("data", data) if isinstance(data, dict) else data
        if as_json:
            print(json.dumps(tasks, indent=2))
            return
        if not tasks:
            print("No tasks found.")
            return
        for t in tasks:
            tid = t.get("id", "?")
            ttype = t.get("type", "?")
            tstatus = t.get("status", "?")
            prio = t.get("priority", "?")
            target = t.get("target_agent_id") or t.get("target_capability") or "broadcast"
            created = t.get("created_at", "n/a")
            print(f"  [{tstatus}] {tid}  {ttype}  (p{prio}, {target}, {created})")


def show_task(task_id: str, as_json: bool = False) -> None:
    """Show a single task by ID (``GET /tasks/{task}``)."""
    base_url, hive_id, _, token = _base_config()

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(
            f"/api/v1/hives/{hive_id}/tasks/{task_id}",
            headers=_headers(token),
        )
        if resp.status_code != 200:
            print(f"Error fetching task: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)
        data = resp.json()
        task = data.get("data", data) if isinstance(data, dict) else data
        if as_json:
            print(json.dumps(task, indent=2))
            return
        if not isinstance(task, dict):
            print(str(task))
            return
        for key in (
            "id", "type", "status", "priority", "target_agent_id",
            "target_capability", "creator_id", "parent_task_id",
            "created_at", "updated_at",
        ):
            if key in task:
                print(f"  {key}: {task[key]}")


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

    list_p = sub.add_parser("list", help="List tasks in the hive (with filters)")
    list_p.add_argument("--status", help="Filter by task status")
    list_p.add_argument("--type", help="Filter by task type")
    list_p.add_argument("--target-agent-id", help="Filter by target agent ID")
    list_p.add_argument("--target-capability", help="Filter by target capability")
    list_p.add_argument("--creator-id", help="Filter by creator ID")
    list_p.add_argument("--parent-task-id", help="Filter by parent task ID")
    list_p.add_argument("--created-after", help="Filter: created at or after (ISO8601)")
    list_p.add_argument("--created-before", help="Filter: created at or before (ISO8601)")
    list_p.add_argument("--q", help="Free-text search")
    list_p.add_argument("--page", type=int, help="Page number (1-based)")
    list_p.add_argument("--per-page", type=int, help="Items per page (max 100)")
    list_p.add_argument("--json", action="store_true", dest="as_json", help="Output raw JSON")

    show_p = sub.add_parser("show", help="Show a single task by ID")
    show_p.add_argument("task_id", help="Task ID")
    show_p.add_argument("--json", action="store_true", dest="as_json", help="Output raw JSON")

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
    elif args.command == "list":
        list_tasks(
            status=args.status,
            task_type=args.type,
            target_agent_id=args.target_agent_id,
            target_capability=args.target_capability,
            creator_id=args.creator_id,
            parent_task_id=args.parent_task_id,
            created_after=args.created_after,
            created_before=args.created_before,
            q=args.q,
            page=args.page,
            per_page=args.per_page,
            as_json=args.as_json,
        )
    elif args.command == "show":
        show_task(args.task_id, as_json=args.as_json)
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
