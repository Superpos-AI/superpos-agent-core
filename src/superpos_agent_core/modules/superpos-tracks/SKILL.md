---
name: superpos-tracks
description: Manage Superpos tracks — list, get, create, patch, and link or unlink issues. Use when work spans multiple issues and you need a container with a spec, a state, and a linked-issues panel; or when standing up a new area of the product (track bootstrap: proposal knowledge entry → track → linked issues).
---

# Superpos Tracks

Tracks are the hive's first-class work containers. A track holds a
**slug**, a **state** (`planning` / `active` / `paused` / `done` /
`archived`), a native **markdown spec**, and a panel of **linked
issues**. The proposal-to-track-to-issues pattern is the canonical
bootstrap for any new area of work.

## When to use it

- A proposal in `docs/proposals/` is ready to become a track — file
  the proposal as a typed knowledge page, create a matching track,
  and link the work-items to it.
- The user asks "what tracks are open in this hive?" or "show me
  what's blocked on track X."
- You need to flip a track's state (planning → active → done) and
  keep the change in a single place rather than scattered across
  issues.
- You want to attach a brand-new issue to an existing track so the
  track's progress column updates.

If the work is a single issue with no long-running spec behind it,
skip this skill — file the issue directly with `superpos-issues`.

## Tools

All commands are on PATH inside the container. They print JSON to
stdout that you can `jq` over.

### Coverage

The CLI covers list / get / create / patch on tracks and link /
unlink on track-issue edges. **State transitions** are driven through
`POST /tracks/{slug}/transition` (not exposed here) and the
dashboard's track editor at `/dashboard/tracks/{slug}`.

### `superpos-tracks list`

Paginated index of tracks in the configured hive. The index omits
`spec` (use `get` to read the spec body).

```bash
superpos-tracks list
superpos-tracks list --status active
```

Flags:
- `--status` — filter by state; one of `planning, active, paused,
  done, archived`. Filtering is enforced **client-side**: the server
  index does not yet filter, so the CLI keeps only rows whose `state`
  matches (the value is also forwarded as a query param for
  forward-compatibility). There is **no `--tag` flag** — the CLI
  rejects unknown arguments.

### `superpos-tracks get <slug>`

Fetch a single track, including the `spec` body. Use this when you
need to read the full markdown spec rather than just the index row.

```bash
superpos-tracks get agent-capabilities
```

### `superpos-tracks create`

Create a new track. `--slug` and `--title` are required. `--spec-file`
reads the spec from a file (useful for proposal-sized bodies);
otherwise the spec starts empty.

```bash
superpos-tracks create \
  --slug agent-capabilities \
  --title "Agent Capabilities" \
  --description "Per-agent superpos-* CLI modules (knowledge, issues, tracks, workflows)" \
  --spec-file docs/proposals/agent-capabilities.md \
  --status active
```

Flags:
- `--slug` — required, 1-100 chars, `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`
- `--title` — required, 1-255 chars (server field: `name`)
- `--description` — optional, max 10000 chars
- `--spec-file` — path to a markdown file, max 200000 chars (server
  field: `spec`)
- `--status` — initial state; one of `planning, active, paused, done,
  archived`. Defaults to `planning`.

`create` is **not idempotent** — a re-run on a hive that already has
the same slug returns 422. To refresh an existing track, use
`patch`.

### `superpos-tracks patch <slug>`

Partial update on name / description / spec. **Slug and state are
not patchable**: slug never changes after create, and state moves
through the `/transition` endpoint (or the dashboard editor).

```bash
superpos-tracks patch agent-capabilities \
  --title "Agent Capabilities (revised)" \
  --spec-file docs/proposals/agent-capabilities-v2.md
```

At least one of `--title`, `--description`, or `--spec-file` must be
provided.

### `superpos-tracks link-issue <track-slug> <issue-id>`

Link an existing issue to a track. The endpoint is idempotent — a
re-link returns 200 with the existing edge rather than creating a
duplicate.

```bash
superpos-tracks link-issue agent-capabilities 01HXYZ...
```

`add-issue` is an alias for `link-issue` (discoverability).

### `superpos-tracks unlink-issue <track-slug> <issue-id>`

Drop the track-issue edge. Returns `{"ok": true, ...}`. The issue
itself is not touched.

```bash
superpos-tracks unlink-issue agent-capabilities 01HXYZ...
```

## Bootstrap runbook (proposal → track → issues)

The full sequence for standing up a new area of work:

1. **File the proposal as a typed knowledge page.** The `## Proposal`
   line in the track spec is a `[[proposal-<track-slug>]]` wikilink
   that resolves to this entry; without it the link is a dead
   reference.

   ```bash
   superpos-knowledge create \
     --type topic \
     --slug proposal-agent-capabilities \
     --title "Agent Capabilities" \
     --summary "Per-agent superpos-* CLI modules wrapping the platform API." \
     --body-file docs/proposals/agent-capabilities.md \
     --tags proposal,track:agent-capabilities,architecture
   ```

2. **Create the track** with the spec body hard-coded to reference
   the wikilink:

   ```bash
   superpos-tracks create \
     --slug agent-capabilities \
     --title "Agent Capabilities" \
     --description "Per-agent superpos-* CLI modules (knowledge, issues, tracks, workflows)" \
     --spec-file docs/proposals/agent-capabilities.md \
     --status active
   ```

3. **File the work-items as issues** with `superpos-issues create`
   (always set a `description` body — title-only issues look
   indistinguishable from unstarted ones on the dashboard). For
   one-shot link-on-create, add `--track-slug` (two-call
   convenience).

4. **Link the issues to the track.** For each issue id:

   ```bash
   superpos-tracks link-issue agent-capabilities 01HXYZ...
   ```

   Or, for new issues, use `superpos-issues create --track-slug
   agent-capabilities ...` which creates + links in one step.

5. **Verify:**

   ```bash
   superpos-tracks get agent-capabilities
   superpos-issues list --state open
   ```

   The track's linked-issues panel on the dashboard reflects state
   transitions on the issues in real time.

## State machine

```
planning ──┬─→ active ──┬─→ paused ─→ active
           │            ├─→ done
           │            └─→ archived
           └─→ archived

paused ─→ active | done | archived
done ─→ archived | active
archived ─→ (terminal)
```

State is mutated through the server's `/transition` endpoint or the
dashboard's track editor, not through the CLI. `create --status` only
sets the **initial** state for a new track; `list --status` filters
the index by state (client-side). Neither flag transitions an
existing track.

## Tips

- **Slug is forever.** Pick a short, stable slug. The bootstrap
  pattern in this repo uses 2-3 character codes (`k1`, `dw`, `reg`)
  for the canonical tracks; longer slugs are fine for project-
  specific work.
- **Spec body is the proposal summary, not the proposal itself.**
  The full design lives in the linked `[[proposal-<slug>]]`
  knowledge page; the track spec should be a one-paragraph
  status / motivation / approach sketch.
- **Link issues, don't paste them in.** A track with linked issues
  gets live status from the issue state machine; a track with
  issues inlined in the spec goes stale the moment the spec
  doesn't match.
- **Re-link is safe, re-create is not.** `link-issue` is idempotent
  (returns the existing edge on a duplicate). `create` is not —
  a duplicate slug returns 422 and you'll see the field-level
  error in the JSON envelope.

## Requirements

- `SUPERPOS_*` env vars (already set in the container)
- `issues.read` for `list` / `get`
- `issues.manage` for `create` / `patch` / `link-issue` /
  `unlink-issue`
