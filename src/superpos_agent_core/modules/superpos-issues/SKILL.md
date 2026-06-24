---
name: superpos-issues
description: Work with Superpos issues — open, triage, transition state, link to tasks/channels, attach files, post/read discussion comments, request approval, and close them. Use when a task references an issue ID or asks you to file/resolve one.
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

### Coverage

The CLI covers the issue lifecycle (list / show / create / update /
transition / close), linking (tasks, channels, **tracks**),
dependencies, attachments, discussion comments, approval requests, and
the issue-type catalogue. Once the superpos-app track-link read API
(AG-8 / superpos-app#882) is deployed, a track link is readable from
both sides: `show` embeds the linked track (`track` / `track_id`), and
the `superpos-tracks` module lists / unlinks track-issue edges. Until
that backend ships those `track` / `track_id` fields are absent from
`show` — use `superpos-tracks list-issues` to read the relation in the
meantime. Known backend gap: there is **no atomic
create-and-link-to-track** call — `create --track-slug` is a CLI-side
two-call flow (create, then link).

### `superpos-issues list`

Paginated index. Useful for "show me what's open" or "find issues of
type X".

```bash
superpos-issues list --state in_progress
superpos-issues list --state open --per-page 5
superpos-issues list --q "auth"           # title substring (case-insensitive)
superpos-issues list --assignee-id <id>
superpos-issues list --issue-type-id <id>
superpos-issues list --page 2 --per-page 50   # advance past the first page
```

Returns the full envelope including `meta.has_more` / `meta.current_page`
so you can decide whether to fetch more — pass the next index via
`--page` to walk the rest.

### `superpos-issues show <issue-id>`

Full issue with relations: type, recent tasks, dependencies, channel,
thread, **track** (the linked track, if any — `track_id` plus a
`{id, slug, name, state}` summary), pending approvals, allowed
transitions.

> **Pending backend deploy:** the `track` / `track_id` fields are only
> populated once the superpos-app track-link read API (AG-8 /
> superpos-app#882) is live. On backends predating that change `show`
> omits them (absent / null), so don't rely on them yet — read the
> link via `superpos-tracks list-issues` until the API ships.

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
`--thread-id`, `--metadata` (JSON object), `--track-slug`.

`--track-slug` links the new issue to a track in one command:

```bash
superpos-issues create \
  --title "Audit the issues CLI" \
  --issue-type-id 01HISSUETYPEXYZ \
  --track-slug agent-capabilities
```

This is a **two-call convenience**, not an atomic backend operation: the
issue-create endpoint has no track field, so the CLI creates the issue
first and then links it (`POST /tracks/{slug}/issues`). On success the
linked issue is re-fetched and printed (so `track` reflects the link).
If the create succeeds but the link fails, the CLI exits non-zero and
prints the **created issue id** to stderr — the issue exists; re-run
`link-track` to retry just the link (nothing is rolled back).

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

### `superpos-issues link-track <issue-id> --track-slug <slug>`

Link an existing issue to a track. The track is addressed by **slug**
(no id lookup needed); the issue is addressed by id. This is the
explicit, callable-by-id alternative to `create --track-slug`.

```bash
superpos-issues link-track 01HXYZ --track-slug agent-capabilities
```

Requires `issues.manage`. Returns `{"track_id", "issue_id"}`.

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

## Attachments (files)

Attach **files** to an issue — screenshots, logs, diffs, repro
artifacts. Files only; there is no URL/link form. `attach` /
`attachments` require the `attachments.read` / `attachments.write`
permissions on the hive. `detach` is destructive and requires the
stronger `attachments.manage` scope, which hosted-agent defaults do
**not** include — without it `detach` returns `403`.

### `superpos-issues attach --issue-id <id> --file <path>`

Upload a local file and link it to the issue.

```bash
superpos-issues attach --issue-id 01HXYZ --file ./repro.png
superpos-issues attach --issue-id 01HXYZ --file ./error.log --description "stack trace from the 502"
```

### `superpos-issues attachments --issue-id <id>`

List the files attached to an issue (returns the full envelope, so you
get `meta` for pagination).

```bash
superpos-issues attachments --issue-id 01HXYZ
superpos-issues attachments --issue-id 01HXYZ --per-page 50
superpos-issues attachments --issue-id 01HXYZ --page 2 --per-page 50   # advance past the first page
```

### `superpos-issues detach <attachment-id>`

Delete an attachment (removes the file from storage and its record).
Destructive — requires the `attachments.manage` scope. Hosted-agent
defaults grant only `attachments.read` / `attachments.write`, so this
command returns `403` for those agents unless `attachments.manage` has
been granted explicitly.

```bash
superpos-issues detach 01HATTACHMENTXYZ
```

## Discussion (comments)

Post and read threaded discussion on an issue. Backed by the hive's
thread API; requires the `threads.read` / `threads.write` permissions.
The first comment on an issue with no thread **transparently creates and
links** a discussion thread — you never manage `thread_id` by hand.

### `superpos-issues comment --issue-id <id> --message <text>`

Append a comment. Auto-creates + links the thread on first use.

```bash
superpos-issues comment --issue-id 01HXYZ --message "Confirmed the repro — patch incoming."
```

### `superpos-issues discussion --issue-id <id>`

Print the issue's full comment history (or a "No discussion yet."
marker if no thread exists).

```bash
superpos-issues discussion --issue-id 01HXYZ
```

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
- `attachments.read` / `attachments.write` for `attach` / `attachments`
- `attachments.manage` (destructive scope) for `detach` — not in hosted-agent defaults
- `threads.read` / `threads.write` for `comment` / `discussion`
