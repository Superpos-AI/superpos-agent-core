---
name: superpos-task
description: Create, schedule, and update Superpos tasks from inside an agent container. Use to spawn subtasks (`create`), set reminders / recurring work (`schedule`), update persona MEMORY (`memory`), and amend an existing task — chiefly to re-target a misrouted task — with `update`.
---

# Superpos Task Helper

`superpos-task` (invoked as `python3 .../superpos_task.py <command>`)
is the in-container CLI for the task queue. It talks to the hive's REST
API using the `SUPERPOS_*` environment already injected into the
container, so you never handle a token.

## When to use it

- Spawn a subtask for delegated / follow-up work → `create`.
- Set a reminder, recurring job, or deferred run → `schedule` /
  `schedules` / `delete-schedule`.
- Persist a lasting fact to persona MEMORY → `memory`.
- **Amend a task that already exists** — re-target a misrouted task,
  bump its priority, extend its timeout, or merge extra payload —
  without losing its id, parent, or trace → `update`.

## Persona + memory doubling (degraded mode)

Persona and MEMORY follow the same **doubling** pattern as modules/skills
(`registry_overlay.py`): Superpos is primary, the agent image carries a
bundled snapshot fallback, re-synced into the workspace on every reachable
startup. Gated by `PLATFORM_PERSONA_MEMORY_DOUBLING` (default ON; set an
explicit falsey value to restore the pre-doubling behaviour).

- **Reads** degrade gracefully: if Superpos is unreachable, the persona and
  the MEMORY default rules are served from the last-known-good snapshot, so
  the agent still boots with an identity.
- **`memory` writes stay Superpos-only.** During an outage a `memory` write
  **fails loudly** (nonzero exit) — there is deliberately no agent-local
  fallback. Degraded mode is read-only by design; do not work around it by
  writing memory elsewhere. Retry the write once Superpos is reachable.

## `superpos-task update <task-id>`

Partially updates a not-yet-terminal task via
`PATCH /api/v1/hives/{hive}/tasks/{task}`. Only the flags you actually
pass are sent, and the backend **shallow-merges** them — any attribute
you omit is left untouched. This is the key to safe partial edits:
pass exactly what you want to change.

Mutable fields (flags map 1:1):

- `--target-agent-id <id>` — pin the task to an agent. Pass an **empty
  string** (`--target-agent-id ''`) to clear it → the task becomes a
  **broadcast** any capable agent can claim.
- `--target-capability <cap>` — route by capability. `''` clears it.
- `--priority <0-4>` — re-prioritise.
- `--payload '<json>'` — JSON object, shallow-merged into the existing
  payload. A `null` value for a key **deletes** that key.
- `--timeout-seconds <int>` — claim timeout.
- `--max-retries <int>` — retry budget.
- `--expires-at <iso8601>` — expiry; `''` clears it.
- `--failure-policy '<json>'` — JSON object.
- `--audit-reason "<why>"` — **recommended**. Recorded verbatim in the
  audit log (sent as the `X-Audit-Reason` header). If omitted, the CLI
  prints a warning to stderr but still runs.

Passing **no** mutable flag is an error (there is nothing to change).
The updated task JSON is printed to stdout.

### Errors

- `422` — you sent an immutable or invalid field.
- `409` — the task is in a terminal state and can no longer be edited.
- `429` — rate limited (60 updates/min per task); retry shortly.

Each maps to a clear stderr message and a nonzero exit code.

### Worked example — redirect a misrouted task

A task was pinned to the wrong agent. Clear the pin so any agent with
the right capability picks it up, and record why:

```bash
# Re-broadcast a misrouted task to a capable agent
superpos-task update 01HX \
  --target-agent-id '' \
  --target-capability data-analysis \
  --audit-reason "wrong target, redirecting to data-analysis pool"
```

Other common edits:

```bash
# Bump priority (e.g. oncall escalation)
superpos-task update 01HX --priority 4 --audit-reason "bumped by oncall"

# Pure broadcast (clear the agent pin, keep everything else)
superpos-task update 01HX --target-agent-id '' --audit-reason "wrong target, redirecting"

# Merge extra payload + drop a stale key (null deletes)
superpos-task update 01HX \
  --payload '{"region":"eu-west-1","stale_flag":null}' \
  --audit-reason "correcting region"
```
