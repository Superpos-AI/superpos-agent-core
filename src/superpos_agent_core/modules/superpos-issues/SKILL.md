---
name: superpos-issues
description: Work with Superpos issues — open, triage, transition state, link to tasks/channels, request approval, and close them. Use when a task references an issue ID or asks you to file/resolve one.
---

# Superpos Issues

Issues are the hive's tracked work items. They have a typed lifecycle
(state machine), can be linked to tasks/channels/threads, can declare
dependencies on other issues, and may require approval to close. Use
this skill whenever a Superpos task tells you to triage, transition,
link, or close an issue — or asks you to file one.

## When to use it

- The incoming task mentions an issue ID (e.g. `issue_id`, `01H…`) and
  asks you to act on it (move state, comment, link the task, close).
- The user asks you to *file* an issue for something you can't fix in
  this run (escalation, blocked work, follow-up).
- A task asks for a status overview ("what issues are open in this
  hive?", "anything blocked?").

If the work doesn't touch an issue — just code, just a one-off task,
just knowledge lookup — skip this skill.

## Tools

All commands are on PATH inside the container. They print JSON to
stdout that you can `jq` over.

### `superpos-issues list`

Paginated index. Useful for "show me what's open" or "find issues of
type X".

```bash
superpos-issues list --state in_progress
superpos-issues list --state open --per-page 5
superpos-issues list --q "auth"           # title substring (case-insensitive)
superpos-issues list --assignee-id <id>
superpos-issues list --issue-type-id <id>
```

Returns the full envelope including `meta.has_more` / `meta.current_page`
so you can decide whether to fetch more.

### `superpos-issues show <issue-id>`

Full issue with relations: type, recent tasks, dependencies, channel,
thread, pending approvals, allowed transitions.

```bash
superpos-issues show 01HXYZ...
```

Read `allowed_transitions` before calling `transition` — the server
returns 422 if you try an illegal transition.

### `superpos-issues create`

File a new issue. `--issue-type-id` is required — look it up first with
`superpos-issues types`.

```bash
superpos-issues create \
  --title "Auth webhook returns 502 on retry" \
  --issue-type-id 01HISSUETYPEXYZ \
  --description "Steps to repro: ..." \
  --metadata '{"source":"telegram"}'
```

Optional flags: `--assignee-type`, `--assignee-id`, `--channel-id`,
`--thread-id`, `--metadata` (JSON object).

### `superpos-issues update <issue-id>`

Partial update — only the flags you pass get touched.

```bash
superpos-issues update 01HXYZ --title "Auth webhook returns 502 (retry)"
superpos-issues update 01HXYZ --assignee-id <agent-id>
superpos-issues update 01HXYZ --metadata '{"severity":"high"}'
```

### `superpos-issues transition <issue-id> --to <state>`

Drive the state machine. Common transitions:

- `open` → `in_progress` (you're picking it up)
- `in_progress` → `awaiting_review` (you finished; waiting for human)
- `in_progress` → `blocked` (you need an approval — see `request-approval`)
- `*` → `done` (closing via the state machine; see also `close`)
- `*` → `cancelled` (won't fix)

```bash
superpos-issues transition 01HXYZ --to in_progress
superpos-issues transition 01HXYZ --to awaiting_review --reason "Patch in PR #42"
```

422 means the transition isn't allowed from the current state — re-run
`show` to see `allowed_transitions`.

### `superpos-issues close <issue-id>`

Policy-aware close. The server consults the issue type's
`closure_policy`: a direct close to `done` happens when allowed;
otherwise the issue lands in `awaiting_review` or `blocked` with a
fresh `ApprovalRequest`. Inspect `state` on the response to learn what
happened.

```bash
superpos-issues close 01HXYZ --reason "Fixed in PR #42, merged to main"
```

### `superpos-issues link-task <issue-id> --task-id <task-id>`

Attach an existing task to the issue. Useful when you spawn a subtask
to address an issue — link it so the work stays traceable.

### `superpos-issues link-channel <issue-id> --channel-id <channel-id>`

Bind a channel (Telegram/Slack thread) to the issue.

### `superpos-issues request-approval <issue-id>`

Escalate for human review. Valid only when the issue is `in_progress`
or `blocked`. Creates a pending `ApprovalRequest` and (from
`in_progress`) moves the issue to `blocked`.

```bash
superpos-issues request-approval 01HXYZ \
  --summary "Closure requires sign-off — affected prod users" \
  --recommended-action approve_closure \
  --risks "Reverting requires a redeploy"
```

### `superpos-issues add-dependency <issue-id> --depends-on <other-issue-id> --kind blocks`

Declare a blocking relationship.

```bash
superpos-issues add-dependency 01HXYZ --depends-on 01HABC --kind blocks
```

### `superpos-issues remove-dependency <issue-id> <dependency-id>`

Drop a dependency by its row ID (returned by `add-dependency` /
visible under `dependencies[]` in `show`).

### `superpos-issues types`

List the hive's issue-type catalogue (id, key, label,
`closure_policy`). Run this once when you need to `create` — you need
the `issue_type_id`.

## Tips

- **Read before you write.** `show` is cheap and tells you the current
  state, allowed transitions, and open approvals. Don't assume.
- **One transition at a time.** The state machine rejects multi-hop
  moves (`open` → `done` isn't legal directly). Walk through
  `in_progress` first.
- **`close` is smarter than `transition --to done`.** It picks the
  right path based on the issue type's closure policy (direct,
  awaiting_review, or create_approval).
- **Link tasks you spawn.** If you create a subtask to work on an
  issue, run `link-task` so the audit trail follows.

## Requirements

- `SUPERPOS_*` env vars (already set in the container)
- `issues.read` and/or `issues.manage` permission on the hive
